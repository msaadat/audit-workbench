"""Durable unified command/action-graph runner."""

from __future__ import annotations

import copy
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from difflib import SequenceMatcher
from pathlib import Path

from .. import (
    analytics, assistant, document_analysis, documents, llm, methodology, templates_store,
    sandbox, validation,
)
from ..workspaces import Workspace, WorkspaceError, slugify
from . import actions, artifact_index, ledger, prompts, store
from .base import BaseRunner, Cancelled, LimitExceeded

GOAL_TEMPLATES = {
    "full_audit_working_draft": {
        "objective": "Execute RCM-linked planned tests through an evidence-linked report working draft.",
        "constraints": ["Do not assert a formal audit opinion.", "Preserve auditor edits."],
    },
    "planning": {"objective": "Prepare or improve engagement planning and structured RCM planned tests."},
    "apm_only": {"objective": "Prepare or revise only the audit planning memorandum."},
    "data_analysis": {"objective": "Analyze available structured data and preserve useful validated work."},
    "document_testing": {"objective": "Prepare and execute relevant document tests and working papers."},
    "report": {"objective": "Prepare evidence-linked audit report working content and run quality checks."},
}

MAX_SOURCE_DOCUMENTS = 8
MAX_PLANNING_DOSSIER_CHARACTERS = 60_000
MAX_PARALLEL_PLANNING_DOCUMENTS = 4
ELIGIBLE_TEXT_STATES = ("extracted", "partial")
_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
_CONTEXT_FIELDS = {
    "objective", "entity", "period", "scope", "materiality",
    "key_contacts", "background_notes",
}
_CONTEXT_LABELS = {
    "objective": "objective",
    "entity": "entity",
    "period": "period",
    "scope": "scope",
    "materiality": "materiality",
    "key contacts": "key_contacts",
    "background notes": "background_notes",
}


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
        # Persisted stage key kept for replay compatibility; the active
        # artifact is now the RCM's structured planned-test collection.
        "work_program": "RCM planned tests",
        "verify": "Traceability",
        "summary": "Planning summary",
    }
    PLANNING_ACTION_TYPES = {
        "update_planning_context", "generate_apm", "edit_apm",
        "create_rcm_row", "edit_rcm_row", "create_rcm_planned_test",
        "edit_rcm_planned_test", "create_procedure", "edit_procedure",
    }

    @staticmethod
    def _planning_context(payload: dict) -> dict:
        value = payload.get("context")
        if not isinstance(value, dict):
            return {}
        return {
            key: field
            for key, field in value.items()
            if key in _CONTEXT_FIELDS and str(field or "").strip()
        }

    @staticmethod
    def _context_fallback(document_analyses: list[dict]) -> dict:
        """Recover labelled planning facts from already-generated summaries.

        This is deliberately narrow: it only accepts the labelled fields emitted
        by the document-analysis contract and never tries to infer facts from raw
        prose. It protects the planning/report chain when a synthesis model returns
        valid JSON with a missing or empty ``context`` object.
        """
        recovered: dict[str, object] = {}
        pattern = re.compile(r"^\s*[-*]?\s*\*\*([^*]+?):\*\*\s*(.+?)\s*$")
        for analysis in document_analyses:
            text = "\n".join(
                str(analysis.get(field) or "")
                for field in ("summary_markdown", "audit_notes_markdown")
            )
            for line in text.splitlines():
                match = pattern.match(line)
                if not match:
                    continue
                label = re.sub(r"\s+", " ", match.group(1).strip().casefold())
                key = _CONTEXT_LABELS.get(label)
                value = match.group(2).strip()
                if key and value and key not in recovered:
                    recovered[key] = value
        return recovered

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
            if self._is_full_audit_goal():
                self._ensure_full_audit_stages()
            self._drive_graph()
            self._finish()
        except Cancelled:
            for action in self.run.get("actions") or []:
                if action["status"] in {
                    "proposed", "ready", "awaiting_input", "awaiting_confirmation", "blocked",
                }:
                    ledger.transition(action, "cancelled")
            ledger.project_legacy_plan(self.run)
            self.run["finished"] = store.utcnow()
            self.run["command"]["status"] = "cancelled"
            context = dict(self.handle.cancel_context or {})
            self.run["cancellation"] = {
                "actor": context.get("actor") or "orchestrator",
                "source": context.get("source") or "checkpoint",
                "reason": context.get("reason"),
                "requested_at": context.get("requested_at"),
                "cancelled_at": self.run["finished"],
            }
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

        Full-audit commands prepare their APM, RCM, and planned tests before
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
            if value.type not in {
                "create_procedure", "edit_procedure", "delete_procedure",
                "generate_working_paper",
            }
        ]

    def _table_profiles(self) -> list[dict]:
        profiles = []
        for name in self.ws.table_names():
            try:
                profiles.append(assistant.table_metadata(self.ws, name))
            except Exception as error:
                self.warn(f"Could not profile '{name}' for command planning: {error}")
        return profiles

    def _interpret(self) -> None:
        self.set_status("interpreting")
        self.set_activity(
            "command.interpret", "Preparing the action plan",
            detail="Reviewing the command, available artifacts, and table schemas…",
        )
        index = artifact_index.build(self.ws)
        command = self.run["command"]
        template = GOAL_TEMPLATES.get(command.get("goal_template"))
        if command.get("goal_template") and template is None:
            raise WorkspaceError("Unknown goal template.")
        base_user = prompts.command_interpreter_user(
            command, template, artifact_index.compact(index), self._catalog(), self.run["limits"],
            assistant.schema_brief(self.ws),
            self._table_profiles(),
            prepared_planning=self.run.get("prepared_planning"),
        )
        attempt_user = base_user
        created = []
        payload = {}
        for attempt in range(SEMANTIC_PROPOSAL_ATTEMPTS):
            if attempt:
                self.set_activity(
                    "command.interpret.repair", "Repairing the action plan",
                    detail="The first proposal did not satisfy the registered action contracts.",
                    attempt=attempt + 1,
                )
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
                self._canonicalize_proposals(proposals)
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
        self.set_activity(
            "command.plan.ready", "Action plan ready",
            detail=f"Prepared {len(created)} action{'s' if len(created) != 1 else ''} for execution.",
            current=0, total=len(created),
        )

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
        if self.run.get("parent_run_id"):
            detail = "Reusing valid document analyses and rebuilding the planning transaction."
            try:
                parent = store.load_run(self.ws, self.run["parent_run_id"])
                if parent.get("error"):
                    detail = f"Retrying after: {parent['error']}"
            except WorkspaceError:
                pass
            self.set_activity("planning.retry", "Retrying engagement planning", detail=detail)
        else:
            self.set_activity(
                "planning.prepare", "Preparing engagement planning",
                detail="Starting the document-grounded planning workflow.",
            )
        self.run.setdefault("context", {})["require_planning_quality"] = True
        snapshot = {
            "planning": copy.deepcopy(self.ws.planning),
            "rcm": copy.deepcopy(self.ws.rcm),
            "rcm_migration": copy.deepcopy(self.ws.rcm_migration),
        }
        self.run["planning_transaction"] = {
            "status": "staging",
            "before": store.write_sidecar(self.ws, self.run["id"], snapshot),
            "started_at": store.utcnow(),
        }
        self.run.setdefault("planning_changes", {
            "apm_updated": 0, "apm_proposed": 0,
            "rcm_created": 0, "rcm_updated": 0, "rcm_preserved": 0,
            "planned_test_created": 0, "planned_test_updated": 0,
            "planned_test_preserved": 0,
        })
        self.save()
        try:
            basis = self.stage_context()
            apm = self.stage_apm(basis)
            rcm = self.stage_rcm(basis, apm)
            self.stage_work_program(basis, rcm)
        except Exception as error:
            # Document analyses created while assembling the basis remain
            # reusable; only active planning mutations are rolled back.
            self.ws.planning = snapshot["planning"]
            self.ws.rcm = snapshot["rcm"]
            self.ws.rcm_migration = snapshot["rcm_migration"]
            self.ws.save()
            self.run["planning_transaction"].update(
                status="rolled_back", finished_at=store.utcnow(), error=str(error)
            )
            self.save()
            raise
        self.run["planning_transaction"].update(
            status="committed", finished_at=store.utcnow(),
            committed_sha1=artifact_index.canonical_sha1({
                "planning": self.ws.planning, "rcm": self.ws.rcm,
            }),
        )
        self.run["prepared_planning"] = {
            "apm": bool(str(self.ws.planning.get("apm_markdown") or "").strip()),
            "rcm_refs": [item["id"] for item in self.ws.rcm],
            "planned_test_refs": [
                planned["id"]
                for row in self.ws.rcm
                for planned in row.get("planned_tests") or []
            ],
            "document_content_included": bool(basis.get("document_content_included")),
        }
        self.run["planning_basis_run_id"] = self.run["id"]
        self.save()

    def stage_context(self) -> dict:
        task = self.add_task(
            "context",
            "planning:context",
            "Assemble planning context",
            "Reviewing workspace data and available documents…",
        )
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
        if not requested_document_ids:
            self.task_detail(task, "Selecting relevant documents for audit planning…")
            requested_document_ids = self._select_planning_documents(task, context)
            requested = set(requested_document_ids)
        document_states = document_analysis.status_catalog(self.ws)["entries"]
        document_metadata = [
            {
                "id": doc.get("id"),
                "title": doc.get("title"),
                "category": doc.get("category"),
                "pages": doc.get("pages"),
                "text_state": doc.get("text_state"),
                **dict(document_states.get(str(doc.get("id"))) or {}),
                "selected_for_update": doc.get("id") in requested,
            }
            for doc in self.ws.documents
        ]
        methodology_query = " ".join(
            str(context.get(key) or "") for key in ("objective", "scope", "background_notes")
        ).strip() or "internal audit risk controls procedures"
        pack_results = methodology.search(self.ws, methodology_query, limit=5)
        methodology_context = []
        included_documents = []
        included_names: list[str] = []
        included_packs: dict[str, dict] = {}
        document_count = len(requested_document_ids)
        analyses_by_id: dict[str, dict | None] = {}
        pending_analyses: list[tuple[int, dict, str]] = []
        cached_analyses = 0
        for document_index, document_id in enumerate(requested_document_ids, start=1):
            doc = next(item for item in self.ws.documents if item.get("id") == document_id)
            document_name = Path(
                str(doc.get("source") or doc.get("title") or document_id)
            ).name
            included_names.append(document_name)
            analysis = document_analysis.compact_artifact(self.ws, document_id)
            if analysis is None:
                pending_analyses.append((document_index, doc, document_name))
            else:
                cached_analyses += 1
                self.task_detail(
                    task,
                    f"Loading document analysis {document_index} of {document_count}: {document_name}",
                )
                analyses_by_id[document_id] = analysis
        if cached_analyses:
            self.task_detail(
                task,
                f"Reusing {cached_analyses} cached "
                f"{'analysis' if cached_analyses == 1 else 'analyses'}; "
                f"{len(pending_analyses)} document{'s' if len(pending_analyses) != 1 else ''} need analysis…",
            )
        if len(pending_analyses) == 1:
            document_index, doc, document_name = pending_analyses[0]
            self.task_detail(
                task,
                f"Analyzing document {document_index} of {document_count}: {document_name}",
            )
            self.set_activity(
                "planning.documents", "Analyzing documents", detail=document_name,
                current=document_index, total=document_count, task_id=task["id"],
            )
            analyses_by_id[doc["id"]] = self._ensure_planning_analysis(doc)
        elif pending_analyses:
            self.task_detail(
                task,
                f"Analyzing {len(pending_analyses)} documents concurrently…",
            )
            self.set_activity(
                "planning.documents", "Analyzing documents",
                detail=f"{len(pending_analyses)} new; {cached_analyses} cached",
                current=cached_analyses, total=document_count, task_id=task["id"],
            )
            workers = min(MAX_PARALLEL_PLANNING_DOCUMENTS, len(pending_analyses))
            with ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix=f"planning-doc-{self.run['id']}",
            ) as executor:
                futures = {
                    executor.submit(self._ensure_planning_analysis, doc): doc
                    for _document_index, doc, _document_name in pending_analyses
                }
                completed_analyses = 0
                for future in as_completed(futures):
                    doc = futures[future]
                    analyses_by_id[doc["id"]] = future.result()
                    completed_analyses += 1
                    self.task_detail(
                        task,
                        f"Analyzed {completed_analyses} of {len(pending_analyses)} new "
                        f"document{'s' if len(pending_analyses) != 1 else ''}; "
                        f"reusing {cached_analyses} cached "
                        f"{'analysis' if cached_analyses == 1 else 'analyses'}…",
                    )
                    self.set_activity(
                        "planning.documents", "Analyzing documents",
                        detail=str(doc.get("title") or doc.get("id") or "Document"),
                        current=cached_analyses + completed_analyses,
                        total=document_count, task_id=task["id"],
                    )
        # Parallel completion order is nondeterministic; build the bounded
        # dossier in the auditor's original selection order.
        for document_id in requested_document_ids:
            doc = next(item for item in self.ws.documents if item.get("id") == document_id)
            analysis = analyses_by_id.get(document_id)
            if analysis is None:
                self.warn(f"Could not obtain a current analysis for '{doc.get('title') or document_id}'; planning will use metadata only.")
                continue
            included_documents.append(analysis)
        for result in pack_results:
            source_ref = f"pack:{result['scope']}:{result['pack_id']}"
            if source_ref not in included_packs:
                included_packs[source_ref] = documents.prompt_content(self.ws, source_ref, [1])
                self.record_model_source(included_packs[source_ref])
            content = included_packs[source_ref]["pages"][0]["text"]
            excerpt = result["excerpt"] if result["excerpt"] in content else ""
            methodology_context.append({**result, "excerpt": excerpt})
        included_documents = self._bounded_dossier(included_documents)
        basis = {
            "planning": copy.deepcopy(self.ws.planning),
            "documents": document_metadata,
            "tables": tables,
            "document_content_included": bool(included_documents),
            "document_analyses": included_documents,
            "methodology_available": [
                {key: pack.get(key) for key in ("id", "name", "scope", "version", "sha1")}
                for pack in methodology.list_packs(self.ws)
            ],
            "methodology": methodology_context,
        }
        if included_documents:
            self.task_detail(
                task,
                f"Synthesizing planning context from {len(included_documents)} "
                f"document{'s' if len(included_documents) != 1 else ''}…",
            )
            self.set_activity(
                "planning.context.synthesis", "Synthesizing planning context",
                detail=f"Using {len(included_documents)} current document analyses.",
                task_id=task["id"],
            )
            self.note_context(
                task,
                f"{', '.join(included_names)} "
                f"({len(included_names)} file{'s' if len(included_names) != 1 else ''})",
            )
            context_user = prompts.document_context_user(context, included_documents)
            payload = self.llm_json(
                prompts.DOCUMENT_CONTEXT_SYSTEM,
                context_user,
                {"representation": "summary", "characters_supplied": sum(
                    len(str(item.get("summary_markdown") or "")) + len(str(item.get("audit_notes_markdown") or ""))
                    for item in included_documents
                ), "context_outcome": "supplied", "cache_hit": True,
                 "document_ids": requested_document_ids,
                 "page_ranges": sorted({citation["page"] for item in included_documents for citation in item.get("citations") or []}),
                 "source_hashes": [item["source_sha1"] for item in included_documents if item.get("source_sha1")]},
            )
            proposed_context = self._planning_context(payload)
            fallback_context = self._context_fallback(included_documents)
            if not proposed_context and fallback_context:
                repair_user = (
                    f"{context_user}\n\nYour previous response omitted every supported planning "
                    "fact even though the labelled document summaries contain them. Return a "
                    "corrected object with a non-empty `context` grounded only in those summaries."
                )
                repaired = self.llm_json(prompts.DOCUMENT_CONTEXT_SYSTEM, repair_user)
                proposed_context = self._planning_context(repaired)
            if not proposed_context and fallback_context:
                proposed_context = fallback_context
                self.warn(
                    "Planning-context synthesis returned no usable fields; recovered labelled "
                    "facts from the persisted document analyses."
                )
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
                        in _CONTEXT_FIELDS
                        and str(value or "").strip()
                    }
                    if accepted_context:
                        self.ws.update_planning({"context": accepted_context}, agent=True)
                        self.record_artifact("planning", "context", "planning:context", "updated", task)
        # Store an immutable provenance snapshot that includes any context just
        # persisted above. Later planning mutations must not rewrite this basis.
        basis["planning"] = copy.deepcopy(self.ws.planning)
        self.run["planning_basis"] = basis
        self.save()
        if task["status"] != "completed":
            self.task_status(task, "completed")
        return basis

    @staticmethod
    def _bounded_dossier(items: list[dict]) -> list[dict]:
        lengths = [
            len(str(item.get("summary_markdown") or ""))
            + len(str(item.get("audit_notes_markdown") or ""))
            + sum(len(str(citation.get("excerpt") or "")) for citation in item.get("citations") or [])
            for item in items
        ]
        allocations = documents._fair_character_allocations(lengths, MAX_PLANNING_DOSSIER_CHARACTERS)
        output = []
        for item, allocation, original_length in zip(items, allocations, lengths, strict=True):
            bounded = dict(item); remaining = allocation
            field_order = ["summary_markdown", "audit_notes_markdown"]
            if item.get("audit_notes_overridden") and not item.get("summary_overridden"):
                field_order.reverse()
            values = {}
            for field in field_order:
                value = str(item.get(field) or "")
                values[field] = value[:remaining]
                remaining -= len(values[field])
            bounded.update(values)
            citations = []
            for citation in item.get("citations") or []:
                excerpt = str(citation.get("excerpt") or "")
                if len(excerpt) > remaining:
                    break
                citations.append(citation); remaining -= len(excerpt)
            bounded["citations"] = citations
            bounded["dossier_trimmed"] = allocation < original_length
            bounded["dossier_characters"] = allocation - remaining
            output.append(bounded)
        return output

    def _ensure_planning_analysis(self, document: dict) -> dict | None:
        """Reuse or create persistent analysis; raw text appears only in map calls."""
        current = document_analysis.compact_artifact(self.ws, document["id"])
        if current is not None:
            return current
        extracted = documents.extract_document(self.ws, document["id"])
        if extracted.get("state") in {"failed", "image_only"}:
            return None
        chunks = document_analysis.analysis_chunks(extracted)
        maps, orientation = [], ""
        for chunk in chunks:
            self.record_model_source({
                "source_ref": document["id"], "document_id": document["id"],
                "source_sha1": document.get("sha1"),
                "pages": [{"page": page} for page in chunk["pages"]],
            })
            # The planning runner uses its existing bounded document-context
            # prompt tag for compatibility with providers that do not support
            # a nested tool loop. This is still the one raw-source map pass.
            payload = self.llm_json(
                prompts.DOCUMENT_CONTEXT_SYSTEM,
                prompts.document_analysis_map_user(document, chunk, orientation),
                {"representation": "raw_pages", "characters_supplied": len(chunk["text"]),
                 "context_outcome": "supplied", "cache_hit": False,
                 "document_ids": [document["id"]], "page_ranges": chunk["pages"],
                 "source_hashes": [document["sha1"]]},
            )
            context_payload = payload.get("context") if isinstance(payload.get("context"), dict) else None
            summary = str(payload.get("summary_markdown") or "").strip()
            if not summary and context_payload:
                summary = "\n".join(f"- **{key.replace('_', ' ').title()}:** {value}" for key, value in context_payload.items())
            mapped = {
                "summary_markdown": summary,
                "audit_notes_markdown": str(payload.get("audit_notes_markdown") or "").strip(),
                "citations": document_analysis.validate_citations(payload.get("citations") or [], [chunk], document["sha1"]),
            }
            maps.append(mapped); orientation = (orientation + "\n\n" + summary)[-4000:]
        if not maps:
            return None
        # Deterministic consolidation keeps workflow-required analysis bounded
        # and avoids rereading source. Explicit analysis runs use the reducer.
        output = {
            "summary_markdown": "\n\n".join(value["summary_markdown"] for value in maps if value["summary_markdown"]),
            "audit_notes_markdown": "\n\n".join(value["audit_notes_markdown"] for value in maps if value["audit_notes_markdown"]),
            "citations": [citation for value in maps for citation in value["citations"]],
        }
        profile = llm.agent_status()
        document_analysis.persist_analysis(
            self.ws, document, extracted, output, provider=profile.get("provider") or profile.get("backend"),
            model=profile.get("model"), action="analyze",
        )
        return document_analysis.compact_artifact(self.ws, document["id"])

    def _select_planning_documents(self, task: dict, context: dict) -> list[str]:
        eligible = [
            doc for doc in self.ws.documents
            if str(doc.get("text_state") or "") in ELIGIBLE_TEXT_STATES
        ]
        if not eligible:
            return []
        document_states = document_analysis.status_catalog(self.ws)["entries"]
        eligible_meta = [
            {
                "id": doc.get("id"),
                "title": doc.get("title"),
                "category": doc.get("category"),
                "pages": doc.get("pages"),
                "text_state": doc.get("text_state"),
                **dict(document_states.get(str(doc.get("id"))) or {}),
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

    def _quality_draft(
        self, system: str, user: str, validator, label: str, *, task: dict | None = None,
    ) -> dict:
        payload = self.llm_json(system, user)
        if not self.context.get("require_planning_quality"):
            return payload
        error = validator(payload)
        if not error:
            return payload
        if task is not None:
            self.task_detail(
                task,
                f"Revising the {label} after quality review: {error}…",
            )
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
    def _apm_quality(payload: dict, template: str, context: dict | None = None) -> str | None:
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
        normalized = re.sub(r"\s+", " ", markdown.casefold())
        context = context or {}
        for field in ("objective", "scope"):
            if context.get(field) and re.search(
                rf"\b{field}\b.{{0,80}}\b(?:not available|not defined|undefined)\b",
                normalized,
            ):
                return f"the memorandum says {field} is unavailable despite structured context"
        return None

    @staticmethod
    def _rcm_quality(payload: dict, existing_ids: set[str] | None = None) -> str | None:
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
            operation = str(row.get("operation") or "").strip().lower()
            if operation and operation not in {"update", "create"}:
                return f"RCM row {index} has an unsupported operation"
            if operation == "update":
                try:
                    row_id = artifact_index.canonical_id(row.get("rcm_id"), "rcm")
                except ValueError:
                    return f"RCM row {index} has an invalid rcm_id"
                if not row_id or existing_ids is not None and row_id not in existing_ids:
                    return f"RCM row {index} does not identify an existing RCM row"
            if operation == "create" and not str(row.get("new_risk_reason") or "").strip():
                return f"RCM row {index} does not explain why the risk is new"
        return None

    @staticmethod
    def _words(value: object) -> set[str]:
        return set(re.findall(r"[a-z0-9]+", str(value or "").casefold()))

    @classmethod
    def _similarity(cls, left: object, right: object) -> float:
        left_text = " ".join(sorted(cls._words(left)))
        right_text = " ".join(sorted(cls._words(right)))
        left_words, right_words = cls._words(left), cls._words(right)
        overlap = len(left_words & right_words) / max(1, len(left_words | right_words))
        return max(overlap, SequenceMatcher(None, left_text, right_text).ratio())

    def _match_rcm_revision(self, spec: dict, semantic: str) -> tuple[dict | None, bool]:
        explicit = artifact_index.canonical_id(
            spec.get("rcm_id") or spec.get("id"), "rcm"
        )
        if explicit:
            return next((row for row in self.ws.rcm if row.get("id") == explicit), None), False
        exact = self.ws.find_semantic("rcm", semantic)
        if exact:
            return exact, False
        narrative = f"{spec.get('process', '')} {spec.get('risk', '')}"
        ranked = sorted(
            (
                (self._similarity(narrative, f"{row.get('process', '')} {row.get('risk', '')}"), row)
                for row in self.ws.rcm
            ),
            key=lambda item: (-item[0], str(item[1].get("id"))),
        )
        if not ranked or ranked[0][0] < 0.72:
            return None, False
        ambiguous = len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.08
        return (None, True) if ambiguous else (ranked[0][1], False)

    def _match_planned_test_revision(
        self, rcm_id: str, spec: dict, semantic: str,
    ) -> tuple[dict | None, bool]:
        row = next((item for item in self.ws.rcm if item.get("id") == rcm_id), None)
        if row is None:
            return None, False
        explicit = artifact_index.canonical_id(
            spec.get("planned_test_id") or spec.get("id"), "planned_test"
        )
        if explicit:
            return next(
                (item for item in row.get("planned_tests") or [] if item.get("id") == explicit),
                None,
            ), False
        exact = next(
            (
                item for item in row.get("planned_tests") or []
                if item.get("semantic_id") == semantic
            ),
            None,
        )
        if exact:
            return exact, False
        narrative = f"{spec.get('title', '')} {spec.get('objective', '')}"
        ranked = sorted(
            (
                (self._similarity(narrative, f"{item.get('title', '')} {item.get('objective', '')}"), item)
                for item in row.get("planned_tests") or []
            ),
            key=lambda item: (-item[0], str(item[1].get("id"))),
        )
        if not ranked or ranked[0][0] < 0.78:
            return None, False
        ambiguous = len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.08
        return (None, True) if ambiguous else (ranked[0][1], False)

    @staticmethod
    def _program_quality(payload: dict) -> str | None:
        procedures = payload.get("planned_tests") or payload.get("procedures") or []
        if not isinstance(procedures, list) or not procedures:
            return "no RCM planned tests were proposed"
        required = ("objective", "criteria", "method", "expected_evidence")
        for index, procedure in enumerate(procedures, start=1):
            if not isinstance(procedure, dict):
                return f"planned test {index} is not an object"
            missing = [key for key in required if not str(procedure.get(key) or "").strip()]
            if missing:
                return f"planned test {index} is missing {missing[0]}"
            if not isinstance(procedure.get("steps"), list) or not any(
                str(value or "").strip() for value in procedure["steps"]
            ):
                return f"planned test {index} has no executable steps"
            if not isinstance(procedure.get("rcm_refs"), list) or not procedure["rcm_refs"]:
                return f"planned test {index} is not linked to an RCM row"
        return None

    def stage_apm(self, basis: dict) -> str:
        self.run.setdefault("planning_changes", {
            "apm_updated": 0, "apm_proposed": 0,
            "rcm_created": 0, "rcm_updated": 0, "rcm_preserved": 0,
            "planned_test_created": 0, "planned_test_updated": 0,
            "planned_test_preserved": 0,
        })
        task = self.add_task("apm", "planning:apm", "Draft the audit planning memorandum")
        if task["status"] == "completed":
            return str(self.ws.planning.get("apm_markdown") or "")
        self.task_status(task, "running")
        self.task_detail(task, "Drafting the APM from the current planning basis…")
        template = templates_store.get_template(self.ws, "apm")["markdown"]
        user = prompts.apm_user(template, basis)
        markdown = self.llm_markdown(
            prompts.APM_SYSTEM, user, legacy_field="apm_markdown"
        )
        if self.context.get("require_planning_quality"):
            planning_context = (basis.get("planning") or {}).get("context") or {}
            error = self._apm_quality(
                {"apm_markdown": markdown}, template, planning_context
            )
            if error:
                self.task_detail(
                    task, f"Revising the APM after quality review: {error}…"
                )
                repair = (
                    f"{user}\n\nThe previous APM draft failed the engagement quality gate: "
                    f"{error}. Return a complete corrected memorandum as Markdown only, without "
                    "a JSON wrapper or Markdown code fence."
                )
                markdown = self.llm_markdown(
                    prompts.APM_SYSTEM, repair, legacy_field="apm_markdown"
                )
                error = self._apm_quality(
                    {"apm_markdown": markdown}, template, planning_context
                )
                if error:
                    raise WorkspaceError(
                        f"The APM draft failed the engagement quality gate: {error}"
                    )
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
            self.task_detail(task, "Applying the APM revision…")
            accepted_markdown = str(accepted[0].get("apm_markdown") or "").strip()
            if not accepted_markdown:
                raise WorkspaceError("The approved APM draft is empty.")
            existing = str(self.ws.planning.get("apm_markdown") or "")
            auditor_owned = (
                existing
                and self.ws.planning.get("created_by") == "user"
                and self.ws.planning.get("agent_run_id") != self.run["id"]
            )
            if auditor_owned and self.run["mode"] != "permission":
                self.run.setdefault("planning_revisions", []).append({
                    "kind": "apm", "status": "proposed",
                    "current_sha1": artifact_index.canonical_sha1(existing),
                    "proposed_markdown": accepted_markdown,
                    "created_at": store.utcnow(),
                })
                self.run["planning_changes"]["apm_proposed"] += 1
                self.save()
                self.warn(
                    "Preserved the auditor-edited APM; the revised draft is stored for review."
                )
            else:
                self.ws.update_planning(
                    {
                        "apm_markdown": accepted_markdown,
                        "created_by": "agent",
                        "agent_run_id": self.run["id"],
                    },
                    agent=True,
                )
                self.run["planning_changes"]["apm_updated"] += 1
                self.record_artifact("planning", "apm", "planning:apm", "updated", task)
        self.task_status(task, "completed")
        return str(self.ws.planning.get("apm_markdown") or markdown)

    def stage_rcm(self, basis: dict, apm: str) -> list[dict]:
        self.run.setdefault("planning_changes", {
            "apm_updated": 0, "apm_proposed": 0,
            "rcm_created": 0, "rcm_updated": 0, "rcm_preserved": 0,
            "planned_test_created": 0, "planned_test_updated": 0,
            "planned_test_preserved": 0,
        })
        task = self.add_task("rcm", "planning:rcm", "Draft the risk and control matrix")
        if task["status"] == "completed":
            return self.ws.rcm
        self.task_status(task, "running")
        existing_count = len(self.ws.rcm)
        self.task_detail(
            task,
            f"Drafting RCM revisions against {existing_count} existing risk"
            f"{'s' if existing_count != 1 else ''}…",
        )
        template = templates_store.get_template(self.ws, "rcm")["markdown"]
        payload = self._quality_draft(
            prompts.RCM_SYSTEM,
            prompts.rcm_user(
                template, self._downstream_planning_basis(basis, "rcm"), apm,
                self.ws.rcm,
            ),
            lambda value: self._rcm_quality(
                value, {str(row.get("id")) for row in self.ws.rcm}
            ),
            "RCM",
            task=task,
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
        accepted_specs = self._accepted_specs("rcm", task, proposals)
        for index, spec in enumerate(accepted_specs, start=1):
            self.set_activity(
                "planning.rcm.apply", "Applying RCM revisions",
                detail=str(spec.get("risk") or "Risk revision"),
                current=index, total=len(accepted_specs), task_id=task["id"],
            )
            if not str(spec.get("risk") or "").strip():
                raise WorkspaceError("An approved RCM row is missing its risk.")
            semantic = str(
                spec.get("semantic_id")
                or f"rcm:{slugify(spec.get('process', ''))}:{slugify(spec['risk'])}"
            )
            spec["semantic_id"] = semantic
            existing, ambiguous = self._match_rcm_revision(spec, semantic)
            operation = str(spec.get("operation") or "").strip().lower()
            if operation == "update" and existing is None and not ambiguous:
                raise WorkspaceError(
                    f"RCM revision target '{spec.get('rcm_id')}' was not found."
                )
            if ambiguous:
                self.warn(
                    f"Skipped ambiguous RCM revision '{spec['risk']}'; choose the durable RCM id."
                )
                continue
            if existing and existing.get("created_by") != "agent" and self.run["mode"] != "permission":
                self.warn(f"Preserved auditor-edited RCM row '{existing['id']}'.")
                self.run["planning_changes"]["rcm_preserved"] += 1
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
                self.run["planning_changes"]["rcm_updated"] += 1
            else:
                item = self.ws.add_rcm({**spec, "agent_run_id": self.run["id"]})
                action = "created"
                self.run["planning_changes"]["rcm_created"] += 1
            self.record_artifact("rcm", item["id"], semantic, action, task)
        self.task_status(task, "completed")
        return self.ws.rcm

    def stage_work_program(self, basis: dict, rcm_rows: list[dict]) -> None:
        self.run.setdefault("planning_changes", {
            "apm_updated": 0, "apm_proposed": 0,
            "rcm_created": 0, "rcm_updated": 0, "rcm_preserved": 0,
            "planned_test_created": 0, "planned_test_updated": 0,
            "planned_test_preserved": 0,
        })
        task = self.add_task("work_program", "planning:work_program", "Draft RCM planned tests")
        if task["status"] == "completed":
            return
        self.task_status(task, "running")
        existing_tests = sum(len(row.get("planned_tests") or []) for row in rcm_rows)
        self.task_detail(
            task,
            f"Drafting planned-test revisions against {existing_tests} existing test"
            f"{'s' if existing_tests != 1 else ''}…",
        )
        template = templates_store.get_template(self.ws, "workpaper")["markdown"]
        payload = self._quality_draft(
            prompts.WORK_PROGRAM_SYSTEM,
            prompts.work_program_user(template, self._downstream_planning_basis(basis, "work_program"), rcm_rows),
            self._program_quality,
            "RCM planned tests",
            task=task,
        )
        proposals = []
        for procedure in payload.get("planned_tests") or payload.get("procedures") or []:
            if not isinstance(procedure, dict) or not str(procedure.get("objective") or "").strip():
                continue
            spec = dict(procedure)
            spec["rcm_refs"] = self._resolve_rcm_refs(
                spec.get("rcm_refs") or [spec.get("rcm_id")]
            )
            if len(spec["rcm_refs"]) != 1:
                self.warn(
                    f"Skipped ambiguous planned test '{spec['objective']}': exactly one RCM parent is required."
                )
                continue
            spec["rcm_id"] = spec["rcm_refs"][0]
            spec["title"] = str(spec.get("title") or spec["objective"])
            spec["semantic_id"] = (
                f"planned-test:{spec['rcm_id']}:{slugify(spec.get('stable_slug') or spec['objective'])}"
            )
            spec["methodology_refs"] = [
                {
                    key: item[key]
                    for key in ("pack_id", "pack_name", "version", "sha1", "section", "citation")
                }
                for item in basis.get("methodology") or []
            ]
            proposals.append(
                self.proposal_item(
                    str(spec["objective"]), "Structured test linked to one draft RCM row.", spec
                )
            )
        accepted_specs = self._accepted_specs("work_program", task, proposals)
        for index, spec in enumerate(accepted_specs, start=1):
            self.set_activity(
                "planning.planned_tests.apply", "Applying planned-test revisions",
                detail=str(spec.get("objective") or "Planned-test revision"),
                current=index, total=len(accepted_specs), task_id=task["id"],
            )
            if not str(spec.get("objective") or "").strip():
                raise WorkspaceError("An approved RCM planned test is missing its objective.")
            refs = self._resolve_rcm_refs(spec.get("rcm_refs") or [spec.get("rcm_id")])
            if len(refs) != 1:
                raise WorkspaceError("An approved RCM planned test must have exactly one RCM parent.")
            rcm_id = refs[0]
            semantic = str(
                spec.get("semantic_id")
                or f"planned-test:{rcm_id}:{slugify(spec.get('stable_slug') or spec['objective'])}"
            )
            spec["semantic_id"] = semantic
            spec["rcm_id"] = rcm_id
            spec["title"] = str(spec.get("title") or spec["objective"])
            existing, ambiguous = self._match_planned_test_revision(rcm_id, spec, semantic)
            operation = str(spec.get("operation") or "").strip().lower()
            if operation == "update" and existing is None and not ambiguous:
                raise WorkspaceError(
                    f"Planned-test revision target '{spec.get('planned_test_id')}' was not found."
                )
            if ambiguous:
                self.warn(
                    f"Skipped ambiguous planned-test revision '{spec['objective']}'; "
                    "choose the durable planned-test id."
                )
                continue
            if existing and existing.get("created_by") != "agent" and self.run["mode"] != "permission":
                self.warn(f"Preserved auditor-edited planned test '{existing['id']}'.")
                self.run["planning_changes"]["planned_test_preserved"] += 1
                continue
            fields = (
                "title", "objective", "criteria", "steps", "method", "expected_evidence",
                "sampling", "thresholds",
                "evidence_refs", "methodology_refs", "result_summary", "conclusion",
                "scope_limitations",
            )
            if existing:
                row, _planned = self.ws.planned_test(existing["id"])
                item = self.ws.update_planned_test(
                    row["id"], existing["id"],
                    {key: spec.get(key) for key in fields if key in spec},
                    agent=True,
                )
                action = "updated"
                self.run["planning_changes"]["planned_test_updated"] += 1
            else:
                item = self.ws.add_planned_test(
                    rcm_id, {**spec, "agent_run_id": self.run["id"]}
                )
                action = "created"
                self.run["planning_changes"]["planned_test_created"] += 1
            self.record_artifact("planned_test", item["id"], semantic, action, task)
        self.task_status(task, "completed")

    @staticmethod
    def _downstream_planning_basis(basis: dict, stage: str) -> dict:
        """Prevent broad document analysis from cascading into later prompts."""
        common = {
            "planning": basis.get("planning"), "tables": basis.get("tables"),
            "documents": basis.get("documents"), "methodology": basis.get("methodology"),
        }
        if stage == "rcm":
            common["document_sources"] = [
                {
                    "document_id": item.get("document_id"), "title": item.get("title"),
                    "source_sha1": item.get("source_sha1"), "analysis_id": item.get("analysis_id"),
                    "coverage": item.get("coverage"), "citations": item.get("citations"),
                }
                for item in basis.get("document_analyses") or []
            ]
        return common

    def _resolve_rcm_refs(self, refs: list) -> list[str]:
        resolved = []
        for ref in refs:
            value = str(ref)
            row = next(
                (
                    item for item in self.ws.rcm
                    if item.get("id") == value
                    or f"rcm:{item.get('id')}" == value
                    or item.get("semantic_id") == value
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
        issues = []
        for row in self.ws.rcm:
            if not row.get("planned_tests"):
                issues.append(f"{row['id']} has no structured planned test.")
        if (self.run.get("planning_basis") or {}).get("document_analyses"):
            recovered = self._context_fallback(self.run["planning_basis"]["document_analyses"])
            if recovered and not any(
                str((self.ws.planning.get("context") or {}).get(key) or "").strip()
                for key in recovered
            ):
                issues.append("Planning documents contain labelled engagement facts, but planning context is empty.")
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
        changes = self.run.get("planning_changes") or {}
        self.run["summary_markdown"] = (
            "# Planning draft summary\n\n"
            f"Revised the audit planning memorandum; RCM changes: "
            f"**{int(changes.get('rcm_created') or 0)} created**, "
            f"**{int(changes.get('rcm_updated') or 0)} updated**, and "
            f"**{int(changes.get('rcm_preserved') or 0)} auditor-owned preserved**. "
            f"The active RCM contains **{len(self.ws.rcm)}** row(s). Planned-test changes: "
            f"**{int(changes.get('planned_test_created') or 0)} created**, "
            f"**{int(changes.get('planned_test_updated') or 0)} updated**, and "
            f"**{int(changes.get('planned_test_preserved') or 0)} auditor-owned preserved**; "
            f"**{sum(len(row.get('planned_tests') or []) for row in self.ws.rcm)}** active structured planned test(s)."
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

    def _ensure_full_audit_stages(self) -> None:
        """Inject mandatory local output/gate phases independently of LLM expansion.

        The model may propose execution and judgment work, but it cannot omit
        roll-up, RCM papers, dashboard curation, report generation, or terminal
        verification from a full-audit graph. Existing matching actions are
        retained so restart/replay remains idempotent.
        """
        existing = self.run.get("actions") or []
        existing_types = {item.get("type") for item in existing}
        existing_papers = {
            str((item.get("target") or {}).get("resolved_id") or "")
            for item in existing
            if item.get("type") == "generate_rcm_working_paper"
        }
        proposals = []
        if "rollup_rcm_results" not in existing_types:
            proposals.append({"id": "mandatory_rcm_rollup", "type": "rollup_rcm_results"})
        for row in self.ws.rcm:
            if row["id"] not in existing_papers:
                proposals.append({
                    "id": f"mandatory_rcm_paper_{slugify(row['id'])}",
                    "type": "generate_rcm_working_paper",
                    "target": {"kind": "rcm", "resolved_id": row["id"]},
                })
        if "curate_dashboard" not in existing_types:
            proposals.append({"id": "mandatory_dashboard", "type": "curate_dashboard"})
        if "generate_report" not in existing_types:
            proposals.append({"id": "mandatory_report", "type": "generate_report", "args": {"use_model": True}})
        if "run_report_quality" not in existing_types:
            proposals.append({"id": "mandatory_report_quality", "type": "run_report_quality"})
        if "verify_audit_completion" not in existing_types:
            proposals.append({"id": "mandatory_completion", "type": "verify_audit_completion"})
        if not proposals:
            return
        available = int(self.run["limits"].get("max_actions", 60)) - len(existing)
        if available <= 0:
            self.run.setdefault("mandatory_stage_issues", []).append(
                "The action limit prevented deterministic full-audit stages from being added."
            )
            self.warn(self.run["mandatory_stage_issues"][-1])
            return
        if len(proposals) > available:
            # Preserve the terminal gates and output phases ahead of per-RCM
            # paper generation when a model has already consumed the budget.
            priority = {
                "rollup_rcm_results": 0, "curate_dashboard": 1,
                "generate_report": 2, "run_report_quality": 3,
                "verify_audit_completion": 4, "generate_rcm_working_paper": 5,
            }
            proposals.sort(key=lambda item: priority[item["type"]])
            proposals = proposals[:available]
            self.run.setdefault("mandatory_stage_issues", []).append(
                "The action limit prevented some RCM working papers from being scheduled."
            )
            self.warn(self.run["mandatory_stage_issues"][-1])
        created = ledger.append_actions(
            self.run, proposals, audit_lifecycle=True
        )
        self.save()
        self.emit(
            "graph_update",
            {"revision": self.run["graph_revision"], "added": [item["id"] for item in created]},
        )

    @staticmethod
    def _proposal_repair_user(base_user: str, payload: dict, error: Exception) -> str:
        validation_guidance = ""
        if "Unknown check" in str(error):
            validation_guidance = (
                " Supported validation check ids are: "
                f"{', '.join(validation.CHECKS)}. Use `required` for null or blank checks."
            )
        analytics_guidance = ""
        if "Unknown analytics test" in str(error):
            analytics_guidance = (
                " Supported analytics test ids are: "
                f"{', '.join(analytics.ANALYTICS)}. Replace every unsupported run_analytics "
                "action with a supported library test or create_custom_analysis; do not only fix "
                "the first named test."
            )
        custom_code_guidance = ""
        if "custom analysis code" in str(error).casefold():
            custom_code_guidance = (
                " Correct every create_custom_analysis snippet, not only the first one. `pl`, "
                "workspace table variables, and `tables['name']` are already available. Remove "
                "all imports and file reads/writes, and assign one summarized DataFrame to `result`."
            )
        return (
            f"{base_user}\n\nYour previous JSON parsed, but its action graph violated the registered "
            f"contract: {error}.{validation_guidance}{analytics_guidance}{custom_code_guidance} "
            f"Return a corrected complete JSON object. "
            f"Preserve the intended goal and "
            f"valid dependencies, use only catalog target kinds and required args. Previous JSON: "
            f"{json.dumps(payload, default=str)}"
        )

    def _canonicalize_proposals(self, proposals: list[object]) -> None:
        """Normalize a proposal batch and report all invented analytics ids."""
        unknown_analytics = set()
        for proposal in proposals:
            if not isinstance(proposal, dict) or proposal.get("type") != "run_analytics":
                continue
            args = proposal.get("args")
            if not isinstance(args, dict):
                continue
            if "test" not in args:
                continue
            test_id = analytics.canonical_test_id(args.get("test"))
            args["test"] = test_id
            if test_id and test_id not in analytics.ANALYTICS:
                unknown_analytics.add(test_id)
        if unknown_analytics:
            names = ", ".join(sorted(unknown_analytics))
            raise WorkspaceError(f"Unknown analytics tests: {names}.")
        for proposal in proposals:
            if isinstance(proposal, dict):
                actions.canonicalize_action_fields(self.ws, proposal)

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
                and definition.risk not in {"read", "compute"}
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
        terminal_statuses = {"succeeded", "failed", "blocked", "skipped", "cancelled"}
        completed_before = sum(
            item.get("status") in terminal_statuses for item in self.run.get("actions") or []
        )
        resolution = action.get("resolution") or {}
        self.set_activity(
            "actions.execute", definition.description,
            detail=str(resolution.get("title") or action["type"]).replace("_", " "),
            current=completed_before + 1,
            total=len(self.run.get("actions") or []),
            attempt=int(action.get("attempts") or 0) + 1,
            action_id=action["id"],
        )
        ledger.transition(action, "running")
        action["attempts"] += 1
        action["error"] = None
        action["finished_at"] = None
        action["prepared_at"] = store.utcnow()
        action["started_at"] = store.utcnow()
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
            custom_repair = action["type"] in {"create_custom_analysis", "edit_custom_analysis"}
            deterministic = isinstance(error, (WorkspaceError, ValueError)) and not custom_repair
            retry = (
                not deterministic
                and action["attempts"] < attempts
                and definition.failure_policy != "stop_run"
            )
            if action["attempts"] < attempts and custom_repair:
                retry = self._repair_custom_analysis(action, error)
            if retry:
                ledger.transition(action, "ready")
            self._save_action(action)
            self.set_activity(
                "actions.retry" if retry else "actions.failed",
                "Retrying an action" if retry else "Action failed; continuing safely",
                detail=str(error),
                current=completed_before + (0 if retry else 1),
                total=len(self.run.get("actions") or []),
                attempt=action["attempts"], action_id=action["id"],
            )
            if (
                action.get("planning_significant")
                and action["status"] == "failed"
                and not deterministic
            ):
                self._expand_after(action, {"error": str(error)})
            elif deterministic:
                self.warn(
                    f"Skipped adaptive replanning after deterministic failure in "
                    f"{action['id']}: {error}"
                )
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
        completed_now = sum(
            item.get("status") in terminal_statuses for item in self.run.get("actions") or []
        )
        self.set_activity(
            "actions.progress", "Action completed",
            detail=definition.description,
            current=completed_now, total=len(self.run.get("actions") or []),
            action_id=action["id"],
        )
        warning = (receipt.get("result") or {}).get("warning")
        if warning:
            self.warn(str(warning))
        for ref in action["result_refs"]:
            kind, _, item_id = ref.partition(":")
            self.emit("workspace_changed", {"kind": kind, "id": item_id, "action": "removed" if definition.risk == "destructive" else "updated"})
        if action.get("planning_significant"):
            self._expand_after(action)

    def _repair_custom_analysis(self, action: dict, error: Exception) -> bool:
        """Replace failed custom code before the executor's one permitted retry."""
        args = action.get("args") or {}
        if action["type"] == "create_custom_analysis":
            spec = args.get("spec") or {}
        else:
            spec = (args.get("changes") or {}).get("spec") or {}
        code = str(spec.get("code") or "")
        try:
            payload = self.llm_json(
                prompts.FIX_CODE_SYSTEM,
                prompts.fix_code_user(
                    code,
                    str(error),
                    {"tables": assistant.schema_brief(self.ws)},
                ),
            )
            repaired = str(payload.get("code") or "").strip()
            if not repaired:
                return False
            # Reject an unsafe/non-result repair now instead of executing the
            # same deterministic failure as a nominal second attempt.
            sandbox.validate(repaired)
        except Exception as repair_error:
            self.warn(f"Custom analysis repair failed for {action['id']}: {repair_error}")
            return False
        spec["code"] = repaired
        return True

    def _expand_after(self, action: dict, safe_result: dict | None = None) -> None:
        if self.run.get("planning_expansion_disabled"):
            return
        usage = self.run["usage"]
        if usage["planner_waves"] >= int(self.run["limits"].get("max_waves", 8)):
            self.warn("Planning-wave limit reached; the current graph will finish without further expansion.")
            return
        remaining_actions = max(
            0,
            int(self.run["limits"].get("max_actions", 60))
            - len(self.run.get("actions") or []),
        )
        if remaining_actions == 0:
            self.run["planning_expansion_disabled"] = True
            self.warn("Action limit reached; adaptive planning is disabled for this run.")
            return
        safe_result = safe_result if safe_result is not None else ((action.get("receipt") or {}).get("result") or {})
        usage["planner_waves"] += 1; self.save()
        failed_result = bool(safe_result.get("error"))
        max_waves = int(self.run["limits"].get("max_waves", 8))
        self.set_activity(
            "command.replan" if failed_result else "command.expand",
            "Replanning after an action failure" if failed_result else "Reviewing results for follow-up work",
            detail=f"Planning wave {usage['planner_waves']} of {max_waves}…",
            current=usage["planner_waves"], total=max_waves, action_id=action["id"],
        )
        index = artifact_index.build(self.ws)
        base_user = prompts.command_planner_user(
            self.run["goal"],
            [{"id": item["id"], "type": item["type"], "status": item["status"], "result_refs": item["result_refs"]} for item in self.run["actions"]],
            [{"action_id": action["id"], "result": safe_result}],
            artifact_index.compact(index), self._catalog(),
            {**self.run["limits"], "remaining_actions": remaining_actions},
            assistant.schema_brief(self.ws),
            self._table_profiles(),
        )
        attempt_user = base_user
        for attempt in range(SEMANTIC_PROPOSAL_ATTEMPTS):
            if attempt:
                self.set_activity(
                    "command.replan.repair", "Repairing the follow-up action plan",
                    detail="The previous follow-up proposal did not satisfy the action contracts.",
                    attempt=attempt + 1, action_id=action["id"],
                )
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
                self._canonicalize_proposals(proposals)
                created = ledger.append_actions(
                    self.run, proposals, depth=action["depth"] + 1,
                    audit_lifecycle=self._is_full_audit_goal(),
                )
            except WorkspaceError as error:
                self._record_rejected_proposals("command_planner", proposals, error)
                if attempt + 1 >= SEMANTIC_PROPOSAL_ATTEMPTS:
                    signature = str(error)
                    failures = self.run.setdefault("planning_failure_signatures", {})
                    failures[signature] = int(failures.get(signature) or 0) + 1
                    if failures[signature] >= 2:
                        self.run["planning_expansion_disabled"] = True
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
            blocked = sum(item["status"] == "blocked" for item in self.run["actions"])
            self.set_activity(
                "actions.blocked", "Blocking dependent actions",
                detail=f"{blocked} action{'s are' if blocked != 1 else ' is'} blocked by earlier failures.",
            )

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
        self.set_activity(
            "command.verify", "Verifying fieldwork results",
            detail="Checking committed, failed, blocked, and skipped actions before summary.",
        )
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
        if self._is_full_audit_goal() and status == "completed":
            verification = next(
                (
                    (item.get("receipt") or {}).get("result") or {}
                    for item in reversed(self.run.get("actions") or [])
                    if item.get("type") == "verify_audit_completion"
                    and item.get("status") == "succeeded"
                ),
                None,
            )
            if verification is None or self.run.get("mandatory_stage_issues"):
                status = "completed_with_issues"
            else:
                status = str(verification.get("status") or "completed_with_issues")
        self.set_status(status); self.emit("summary_ready", {"run_id": self.run["id"]})
