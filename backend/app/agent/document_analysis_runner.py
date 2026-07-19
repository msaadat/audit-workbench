"""Durable map/reduce analysis for selected engagement documents."""

from __future__ import annotations

import os

from .. import document_analysis, documents, llm
from ..workspaces import Workspace, WorkspaceError
from . import prompts, store
from .base import BaseRunner, Cancelled, LimitExceeded


class DocumentAnalysisRunner(BaseRunner):
    stage_titles = {"analyze": "Document analysis", "verify": "Coverage and citations"}

    def checkpoint(self) -> None:
        current = getattr(self, "current_document_id", None)
        if current and self.handle.pause_requested.is_set():
            document_analysis.set_run_state(self.ws, current, "paused", run_id=self.run["id"], resumable=True)
        super().checkpoint()
        if current:
            document_analysis.set_run_state(self.ws, current, "analyzing", run_id=self.run["id"])

    def execute(self) -> None:
        if not self.run.get("started"):
            self.run["started"] = store.utcnow()
        state = self.run.setdefault("document_analysis", {
            "document_ids": list(self.context.get("document_ids") or []),
            "action": str(self.context.get("action") or "analyze"), "documents": {},
        })
        current_document_id: str | None = None
        try:
            self.set_status("executing")
            total_documents = len(state["document_ids"])
            for document_index, document_id in enumerate(state["document_ids"], start=1):
                current_document_id = str(document_id)
                self.current_document_id = current_document_id
                self._analyze_one(
                    current_document_id, state,
                    document_index=document_index, total_documents=total_documents,
                )
                if self.run["status"] == "paused":
                    return
                current_document_id = None
                self.current_document_id = None
            self.run["summary_markdown"] = (
                "# Document analysis\n\n"
                f"Analyzed **{len(state['document_ids'])}** document(s). Generated content "
                "is awaiting auditor review and is not evidence that controls operated."
            )
            self.run["finished"] = store.utcnow(); self.save()
            self.set_status("completed")
            self.emit("summary_ready", {"run_id": self.run["id"]})
        except Cancelled:
            if current_document_id:
                document_analysis.set_run_state(self.ws, current_document_id, "cancelled")
            self.run["finished"] = store.utcnow(); self.set_status("cancelled")
        except LimitExceeded as error:
            self.run["error"] = str(error); self.run["finished"] = store.utcnow(); self.set_status("failed")
        except Exception as error:
            for document_id in ([current_document_id] if current_document_id else []):
                try:
                    document_analysis.set_run_state(self.ws, document_id, "failed")
                    task = self._task(f"document-analysis:{document_id}")
                    if task is not None:
                        self.task_status(task, "failed", str(error))
                except Exception:
                    pass
            self.run["error"] = str(error); self.run["finished"] = store.utcnow(); self.set_status("failed")

    def _analyze_one(
        self, document_id: str, state: dict, *, document_index: int, total_documents: int,
    ) -> None:
        document = next((item for item in self.ws.documents if item.get("id") == document_id), None)
        if document is None:
            raise WorkspaceError(f"Document '{document_id}' not found.")
        task = self.add_task("analyze", f"document-analysis:{document_id}", f"Analyze {document.get('title') or document_id}")
        if task["status"] == "completed":
            return
        self.task_status(task, "running")
        self.set_activity(
            "document_analysis.document", "Analyzing documents",
            detail=str(document.get("title") or document_id),
            current=document_index, total=total_documents, task_id=task["id"],
        )
        extracted = documents.extract_document(self.ws, document_id)
        if extracted.get("state") in {"failed", "image_only"}:
            document_analysis.set_run_state(self.ws, document_id, "failed")
            raise WorkspaceError(extracted.get("error") or "The document has no extractable text.")
        chunks = document_analysis.analysis_chunks(extracted)
        progress = state.setdefault("documents", {}).setdefault(document_id, {"completed_chunks": {}, "artifact_id": None})
        document_analysis.set_run_state(self.ws, document_id, "analyzing", run_id=self.run["id"])
        try:
            limit = max(0, int(os.environ.get("DOCUMENT_ANALYSIS_PAGE_LIMIT") or 0))
        except ValueError:
            limit = 0
        completed_before = bool(progress["completed_chunks"])
        allowed_pages = set(sorted({chunk["page"] for chunk in chunks})[:limit]) if limit and not completed_before else None
        orientation = "\n\n".join(
            str(value.get("summary_markdown") or "") for _, value in sorted(progress["completed_chunks"].items())
        )[-4000:]
        for chunk_index, chunk in enumerate(chunks, start=1):
            if chunk["id"] in progress["completed_chunks"]:
                continue
            pages = chunk.get("pages") or []
            page_label = (
                f"pages {pages[0]}–{pages[-1]}" if len(pages) > 1
                else f"page {pages[0]}" if pages else "document text"
            )
            self.task_detail(
                task,
                f"Analyzing document {document_index} of {total_documents}; "
                f"section {chunk_index} of {len(chunks)} ({page_label})…",
            )
            if allowed_pages is not None and chunk["page"] not in allowed_pages:
                break
            self.checkpoint()
            payload = self.llm_json(
                prompts.DOCUMENT_ANALYSIS_MAP_SYSTEM,
                prompts.document_analysis_map_user(document, chunk, orientation),
                {"representation": "raw_pages", "characters_supplied": len(chunk["text"]),
                 "context_outcome": "supplied", "cache_hit": False,
                 "document_ids": [document_id], "page_ranges": chunk["pages"],
                 "source_hashes": [document["sha1"]]},
            )
            validated = document_analysis.validate_citations(payload.get("citations") or [], [chunk], document["sha1"])
            mapped = {"chunk_id": chunk["id"], "pages": chunk["pages"],
                      "summary_markdown": str(payload.get("summary_markdown") or ""),
                      "audit_notes_markdown": str(payload.get("audit_notes_markdown") or ""),
                      "citations": validated}
            progress["completed_chunks"][chunk["id"]] = mapped
            orientation = (orientation + "\n\n" + mapped["summary_markdown"])[-4000:]
            self.save()
        maps = [progress["completed_chunks"][chunk["id"]] for chunk in chunks if chunk["id"] in progress["completed_chunks"]]
        self.task_detail(
            task,
            f"Consolidating document {document_index} of {total_documents} from "
            f"{len(maps)} analyzed section{'s' if len(maps) != 1 else ''}…",
        )
        if not maps:
            raise WorkspaceError("No extractable source chunks were available for analysis.")
        reduced = self.llm_json(
            prompts.DOCUMENT_ANALYSIS_REDUCE_SYSTEM,
            prompts.document_analysis_reduce_user(document, maps),
            {"representation": "summary", "characters_supplied": sum(
                len(mapped["summary_markdown"]) + len(mapped["audit_notes_markdown"]) for mapped in maps
            ), "context_outcome": "supplied", "cache_hit": False,
             "document_ids": [document_id], "page_ranges": sorted({page for mapped in maps for page in mapped["pages"]}),
             "source_hashes": [document["sha1"]]},
        ) if len(maps) > 1 else maps[0]
        citations = [citation for mapped in maps for citation in mapped["citations"]]
        finished_ids = set(progress["completed_chunks"])
        omitted = sorted({chunk["page"] for chunk in chunks if chunk["id"] not in finished_ids})
        analyzed = sorted({chunk["page"] for chunk in chunks if chunk["id"] in finished_ids})
        coverage = {"state": "partial" if omitted else "complete", "analyzed_pages": analyzed,
                    "omitted_pages": omitted, "reason": "configured_page_limit" if omitted else None}
        profile = llm.agent_status()
        analysis = document_analysis.persist_analysis(
            self.ws, document, extracted,
            {"summary_markdown": reduced.get("summary_markdown"),
             "audit_notes_markdown": reduced.get("audit_notes_markdown"), "citations": citations},
            provider=profile.get("provider"), model=profile.get("model"),
            action=str(state.get("action") or "analyze"), coverage=coverage,
        )
        progress["artifact_id"] = (analysis.get("candidate") or analysis.get("effective") or {}).get("id")
        task["result_refs"] = [f"document_analysis:{progress['artifact_id']}"] if progress["artifact_id"] else []
        if omitted:
            document_analysis.set_run_state(self.ws, document_id, "paused", run_id=self.run["id"], resumable=True)
            self.run["status"] = "paused"; self.save()
            self.emit("run_status", {"status": "paused"})
            self.note_context(task, f"Partial coverage: {len(analyzed)} analyzed page(s), {len(omitted)} omitted page(s).")
            return
        document_analysis.set_run_state(self.ws, document_id, "idle")
        self.task_status(task, "completed")
        self.emit("workspace_changed", {"kind": "document_analysis", "id": document_id})
