"""Planning interview and draft-generation runner."""

from __future__ import annotations

from .. import assistant, documents, llm, methodology, templates_store
from ..workspaces import WorkspaceError, slugify
from . import prompts, store
from .base import BaseRunner, Cancelled, LimitExceeded

MAX_INTERVIEW_TURNS = 10
SKIP_PHRASES = ("skip interview", "skip the interview", "conclude interview", "end interview")


class PlanningRunner(BaseRunner):
    stage_titles = {
        "interview": "Planning interview",
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
        self.run.setdefault("interview", {"captured": {}, "turns": 0, "pending_question": None})
        try:
            self.set_status("executing")
            self.stage_interview()
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

    def stage_interview(self) -> None:
        task = self.add_task("interview", "planning:interview", "Capture the planning basis")
        if task["status"] == "completed":
            return
        self.task_status(task, "running")
        interview = self.run["interview"]
        if self.context.get("skip_interview"):
            self.task_status(task, "completed")
            return
        template = templates_store.get_template(self.ws, "interview")["markdown"]
        while int(interview.get("turns") or 0) < MAX_INTERVIEW_TURNS:
            self.checkpoint()
            payload = self.llm_json(
                prompts.INTERVIEW_SYSTEM,
                prompts.interview_user(
                    template,
                    self.ws.planning,
                    self.run.get("messages", []),
                    int(interview.get("turns") or 0) + 1,
                ),
            )
            captured = payload.get("captured")
            if isinstance(captured, dict):
                interview.setdefault("captured", {}).update(captured)
                self._merge_captured(captured)
            action = str(payload.get("action") or "ask").lower()
            question = str(payload.get("question") or "").strip()
            if action == "conclude" or not question:
                break
            answer = self.wait_for_input(question)
            interview["turns"] = int(interview.get("turns") or 0) + 1
            answers = interview.setdefault("captured", {}).setdefault("answers", {})
            answers[question] = answer
            self.ws.planning["context"].setdefault("interview_answers", {})[question] = answer
            self.ws.update_planning({"context": self.ws.planning["context"]}, agent=True)
            self.save()
            if any(phrase in answer.lower() for phrase in SKIP_PHRASES):
                break
        interview["pending_question"] = None
        for message in self.run.get("messages", []):
            if message.get("role") == "user":
                message["handled"] = True
        self.save()
        self.task_status(task, "completed")

    def _merge_captured(self, captured: dict) -> None:
        known = {"objective", "entity", "period", "scope", "materiality", "key_contacts", "background_notes"}
        changes = {key: value for key, value in captured.items() if key in known}
        if changes:
            self.ws.update_planning({"context": changes}, agent=True)

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
        document_metadata = [
            {
                "id": doc.get("id"),
                "title": doc.get("title"),
                "category": doc.get("category"),
                "pages": doc.get("pages"),
                "text_state": doc.get("text_state"),
            }
            for doc in self.ws.documents
        ]
        context = self.ws.planning.get("context") or {}
        methodology_query = " ".join(
            str(context.get(key) or "") for key in ("objective", "scope", "background_notes")
        ).strip() or "internal audit risk controls procedures"
        pack_results = methodology.search(self.ws, methodology_query, limit=5)
        methodology_context = []
        disclosed_packs: dict[str, dict] = {}
        if self.ws.settings.get("doc_llm_optin"):
            for result in pack_results:
                source_ref = f"pack:{result['scope']}:{result['pack_id']}"
                if source_ref not in disclosed_packs:
                    disclosed_packs[source_ref] = documents.disclosable_content(
                        self.ws, source_ref, "planning_methodology", self.run["id"], [1]
                    )
                content = disclosed_packs[source_ref]["pages"][0]["text"]
                excerpt = result["excerpt"] if result["excerpt"] in content else ""
                methodology_context.append({**result, "excerpt": excerpt})
        basis = {
            "planning": self.ws.planning,
            "interview": self.run.get("interview", {}),
            "documents": document_metadata,
            "tables": tables,
            "document_content_disclosed": False,
            "methodology_available": [
                {key: pack.get(key) for key in ("id", "name", "scope", "version", "sha1")}
                for pack in methodology.list_packs(self.ws)
            ],
            "methodology": methodology_context,
        }
        self.run["planning_basis"] = basis
        self.save()
        if task["status"] != "completed":
            self.task_status(task, "completed")
        return basis

    def _accepted_specs(self, kind: str, task: dict, proposals: list[dict]) -> list[dict]:
        if self.run["mode"] == "permission":
            return [item["spec"] for item in self.request_approval(kind, task, proposals)]
        return [item["spec"] for item in proposals]

    def stage_apm(self, basis: dict) -> str:
        task = self.add_task("apm", "planning:apm", "Draft the audit planning memorandum")
        if task["status"] == "completed":
            return str(self.ws.planning.get("apm_markdown") or "")
        self.task_status(task, "running")
        template = templates_store.get_template(self.ws, "apm")["markdown"]
        payload = self.llm_json(prompts.APM_SYSTEM, prompts.apm_user(template, basis))
        markdown = str(payload.get("apm_markdown") or "").strip()
        if not markdown:
            raise WorkspaceError("The model returned an empty APM draft.")
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
                    {"apm_markdown": accepted_markdown, "created_by": "agent", "agent_run_id": self.run["id"], "status": "draft"},
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
        payload = self.llm_json(prompts.RCM_SYSTEM, prompts.rcm_user(template, basis, apm))
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
                changes = {key: spec.get(key) for key in ("process", "risk", "risk_rating", "assertion", "control", "control_type", "test_procedure", "test_refs")}
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
        payload = self.llm_json(
            prompts.WORK_PROGRAM_SYSTEM,
            prompts.work_program_user(template, basis, rcm_rows),
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
            fields = ("rcm_refs", "objective", "criteria", "steps", "method", "expected_evidence", "test_refs", "evidence_refs", "methodology_refs", "result_summary", "conclusion", "scope_limitations")
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
            "All outputs remain drafts until the auditor finalizes them."
        )
        self.save()
        self.task_status(task, "completed")
