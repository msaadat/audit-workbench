"""Planning draft-generation runner."""

from __future__ import annotations

import re
from pathlib import Path

from .. import assistant, documents, llm, methodology, templates_store
from ..workspaces import WorkspaceError, slugify
from . import prompts, store
from .base import BaseRunner, Cancelled, LimitExceeded

MAX_SOURCE_DOCUMENTS = 8
MAX_PAGES_PER_SOURCE_DOCUMENT = 50
ELIGIBLE_TEXT_STATES = ("extracted", "partial")
_PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def fill_unavailable_placeholders(markdown: str) -> str:
    """Replace any template placeholder the model left unfilled (e.g. ``{{entity}}``)
    with a plain-language note so the reader sees the context was not available
    rather than a raw token."""
    def replace(match: re.Match) -> str:
        label = match.group(1).replace("_", " ").strip()
        return f"_[{label} — context not available]_"

    return _PLACEHOLDER.sub(replace, markdown)


class PlanningRunner(BaseRunner):
    stage_titles = {
        "context": "Planning context",
        "apm": "Audit planning memorandum",
        "rcm": "Risk and control matrix",
        "work_program": "Audit program",
        "verify": "Traceability",
        "summary": "Planning summary",
    }

    def execute(self) -> None:
        if not self.run.get("started"):
            self.run["started"] = store.utcnow()
        try:
            self.set_status("executing")
            basis = self.stage_context()
            apm = self.stage_apm(basis)
            rcm = self.stage_rcm(basis, apm)
            self.stage_work_program(basis, rcm)
            self.stage_verify()
            self.stage_summary()
            self.run["finished"] = store.utcnow()
            self.set_status("completed")
            self.emit("summary_ready", {"run_id": self.run["id"]})
        except Cancelled:
            self.run["finished"] = store.utcnow()
            self.set_status("cancelled")
        except (LimitExceeded, llm.LLMError, WorkspaceError) as error:
            self.run["error"] = str(error)
            self.run["finished"] = store.utcnow()
            self.set_status("failed")
        except Exception as error:
            self.run["error"] = str(error)
            self.run["finished"] = store.utcnow()
            self.set_status("failed")

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
        # When the caller did not curate a document set, let the agent pick the
        # relevant imported documents so their content grounds the planning
        # basis. Auto mode uses the selection directly; permission mode confirms
        # it with the auditor before any content is disclosed.
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
                "Document AI is off; the planning run can use imported document metadata but not document content."
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
        """Have the agent choose relevant imported documents from metadata only.

        Auto mode applies the selection directly; permission mode surfaces it as
        an approval gate so the auditor confirms the set before any document
        content is disclosed. Returns the confirmed document ids.
        """
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
        # The disclosed filenames are surfaced on the task once content is
        # actually disclosed in stage_context, so no separate note here.
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
        required = ("process", "risk", "risk_rating", "assertion", "control", "control_type", "test_procedure")
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
            prompts.APM_SYSTEM, prompts.apm_user(template, basis),
            lambda value: self._apm_quality(value, template), "APM",
        )
        markdown = str(payload.get("apm_markdown") or "").strip()
        if not markdown:
            raise WorkspaceError("The model returned an empty APM draft.")
        markdown = fill_unavailable_placeholders(markdown)
        proposals = [self.proposal_item("Audit planning memorandum", "Drafted from the current planning basis.", {"apm_markdown": markdown})]
        accepted = self._accepted_specs("apm", task, proposals)
        if accepted:
            accepted_markdown = str(accepted[0].get("apm_markdown") or "").strip()
            if not accepted_markdown:
                raise WorkspaceError("The approved APM draft is empty.")
            existing = str(self.ws.planning.get("apm_markdown") or "")
            if existing and self.ws.planning.get("created_by") == "user" and self.ws.planning.get("agent_run_id") != self.run["id"]:
                self.warn("Preserved the auditor-edited APM; the rerun draft was not applied.")
            else:
                self.ws.update_planning(
                    {"apm_markdown": accepted_markdown, "created_by": "agent", "agent_run_id": self.run["id"]},
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
            prompts.RCM_SYSTEM, prompts.rcm_user(template, basis, apm),
            self._rcm_quality, "RCM",
        )
        proposals = []
        for row in payload.get("rows") or []:
            if not isinstance(row, dict) or not str(row.get("risk") or "").strip():
                continue
            spec = dict(row)
            spec["semantic_id"] = f"rcm:{slugify(spec.get('process', ''))}:{slugify(spec['risk'])}"
            proposals.append(self.proposal_item(str(spec["risk"]), "Proposed planning risk and response.", spec))
        for spec in self._accepted_specs("rcm", task, proposals):
            if not str(spec.get("risk") or "").strip():
                raise WorkspaceError("An approved RCM row is missing its risk.")
            semantic = str(spec.get("semantic_id") or f"rcm:{slugify(spec.get('process', ''))}:{slugify(spec['risk'])}")
            spec["semantic_id"] = semantic
            existing = self.ws.find_semantic("rcm", semantic)
            if existing and existing.get("created_by") != "agent":
                self.warn(f"Preserved auditor-edited RCM row '{existing['id']}'.")
                continue
            if existing:
                changes = {key: spec.get(key) for key in ("process", "risk", "risk_rating", "assertion", "control", "control_type", "test_procedure")}
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
            prompts.WORK_PROGRAM_SYSTEM, prompts.work_program_user(template, basis, rcm_rows),
            self._program_quality, "audit program",
        )
        proposals = []
        for procedure in payload.get("procedures") or []:
            if not isinstance(procedure, dict) or not str(procedure.get("objective") or "").strip():
                continue
            spec = dict(procedure)
            spec["semantic_id"] = f"procedure:{slugify(spec.get('stable_slug') or spec['objective'])}"
            spec["rcm_refs"] = self._resolve_rcm_refs(spec.get("rcm_refs") or [])
            spec["methodology_refs"] = [
                {key: item[key] for key in ("pack_id", "pack_name", "version", "sha1", "section", "citation")}
                for item in basis.get("methodology") or []
            ]
            proposals.append(self.proposal_item(str(spec["objective"]), "Procedure linked to the draft RCM.", spec))
        for spec in self._accepted_specs("work_program", task, proposals):
            if not str(spec.get("objective") or "").strip():
                raise WorkspaceError("An approved procedure is missing its objective.")
            semantic = str(spec.get("semantic_id") or f"procedure:{slugify(spec.get('stable_slug') or spec['objective'])}")
            spec["semantic_id"] = semantic
            spec["rcm_refs"] = self._resolve_rcm_refs(spec.get("rcm_refs") or [])
            existing = self.ws.find_semantic("procedures", semantic)
            if existing and existing.get("created_by") != "agent":
                self.warn(f"Preserved auditor-edited procedure '{existing['id']}'.")
                continue
            fields = ("rcm_refs", "objective", "criteria", "steps", "method", "expected_evidence", "evidence_refs", "methodology_refs", "result_summary", "conclusion", "scope_limitations")
            if existing:
                item = self.ws.update_procedure(existing["id"], {key: spec.get(key) for key in fields if key in spec}, agent=True)
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
            row = next((item for item in self.ws.rcm if item.get("id") == value or item.get("semantic_id") == value), None)
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
