"""Document, AI-activity, and methodology-pack endpoints."""

from __future__ import annotations

import asyncio
import hashlib

from fastapi import APIRouter, Body, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from .. import document_analysis, document_search, documents, embedding, intake, methodology, workspaces
from ..agent import runner, store

router = APIRouter(prefix="/api/workspaces/{workspace_id}", tags=["documents"])


def _ws(workspace_id: str):
    return workspaces.load_workspace(workspace_id)


def _pages(value: str | None) -> list[int] | None:
    if not value:
        return None
    try:
        return [int(part) for part in value.split(",") if part.strip()]
    except ValueError as error:
        raise workspaces.WorkspaceError("Pages must be comma-separated integers.") from error


@router.get("/documents")
async def list_documents(workspace_id: str):
    ws = _ws(workspace_id)
    document_search.recover_indexing(ws)
    return {"items": document_analysis.inventory(ws)}


@router.post("/documents")
async def upload_documents(
    workspace_id: str,
    files: list[UploadFile] = File(...),
    category: str = Form("other"),
    replace: bool = Form(False),
):
    ws = _ws(workspace_id)
    uploads = [(file.filename or "document", await file.read()) for file in files]
    normalized_names = [name.casefold() for name, _ in uploads]
    if len(normalized_names) != len(set(normalized_names)):
        raise workspaces.WorkspaceError("Upload each document filename only once per batch.")
    conflicts = [
        name for name, _ in uploads
        if documents.document_by_source(ws, name) is not None
    ]
    if conflicts and not replace:
        names = ", ".join(conflicts)
        raise workspaces.WorkspaceError(
            f"Document(s) already exist: {names}. Confirm replacement to overwrite them."
        )
    added = []
    replaced = []
    for filename, content in uploads:
        existed = documents.document_by_source(ws, filename) is not None
        document = documents.add_document(
            ws, filename, content, category=category, replace=replace,
        )
        (replaced if existed else added).append(document)
    changed = [*added, *replaced]
    indexing_job = document_search.enqueue_indexing(
        ws, [document["id"] for document in changed], reason="upload",
    )
    runner.notify_evidence_available(
        ws,
        document_ids=[document["id"] for document in changed],
        reason="document_uploaded",
    )
    return {
        "added": added,
        "replaced": replaced,
        "items": document_analysis.inventory(ws),
        "indexing_job": indexing_job,
        "suggested_actions": intake.planning_actions_for_documents(changed),
    }


@router.post("/documents/search")
async def search_documents(workspace_id: str, payload: dict = Body(...)):
    ws = _ws(workspace_id)
    query = str(payload.get("query") or "")
    result = await asyncio.to_thread(
        document_search.search, ws, query,
        document_ids=payload.get("document_ids"), top_k=payload.get("top_k") or 6,
        max_characters=payload.get("max_characters") or 8_000,
    )
    documents.append_activity(
        ws, run_id=payload.get("run_id"), stage=payload.get("stage") or "document_search",
        task=None, purpose=payload.get("purpose") or "document_search", provider=None, model=None,
        vision_used=False, prompt_version=None, template_versions=[], knowledge_packs=[],
        document_ids=sorted({item["document_id"] for item in result["results"]}),
        page_ranges=sorted({item["page"] for item in result["results"]}),
        source_hashes=sorted({item["citation"]["source_sha1"] for item in result["results"]}),
        response_at=documents.utcnow(), response_hash=None, artifact_ref=None, disposition="retrieved",
        representation="excerpt", search_query_hash=hashlib.sha1(query.encode()).hexdigest(),
        characters_supplied=result["characters"], cache_hit=True,
        retrieval_duration_ms=result["duration_ms"], model_duration_ms=None,
        context_outcome="trimmed" if result["trimmed"] else ("supplied" if result["results"] else "unavailable"),
    )
    return result


@router.get("/documents/search-status")
async def document_search_status(workspace_id: str):
    ws = _ws(workspace_id)
    return {"embedding": embedding.status(), "queue": document_search.queue_status(ws), "documents": [
        document_search.manifest_state(ws, document["id"]) for document in ws.documents
    ]}


@router.get("/documents/indexing-status")
async def document_indexing_status(workspace_id: str):
    return document_search.queue_status(_ws(workspace_id))


@router.post("/documents/reindex")
async def reindex_documents(workspace_id: str, payload: dict = Body(...)):
    return await asyncio.to_thread(
        document_search.reindex_job, _ws(workspace_id), payload.get("document_ids") or []
    )


@router.post("/documents/analysis-runs")
async def create_document_analysis_run(workspace_id: str, payload: dict = Body(...)):
    ws = _ws(workspace_id)
    try:
        return await asyncio.to_thread(
            runner.start_run, ws, payload.get("mode") or "auto",
            {"document_ids": payload.get("document_ids") or [], "action": payload.get("action") or "analyze"},
            kind="document_analysis",
        )
    except runner.AgentBusyError as error:
        raise HTTPException(409, detail=str(error)) from error


@router.get("/documents/{doc_id}")
async def get_document(workspace_id: str, doc_id: str):
    ws = _ws(workspace_id)
    return documents.preview(ws, doc_id, None)


@router.get("/documents/{doc_id}/analysis")
async def get_document_analysis(workspace_id: str, doc_id: str):
    return document_analysis.load_analysis(_ws(workspace_id), doc_id)


@router.post("/documents/{doc_id}/analysis-runs")
async def create_single_document_analysis_run(workspace_id: str, doc_id: str, payload: dict = Body(default={})):
    ws = _ws(workspace_id)
    try:
        return await asyncio.to_thread(
            runner.start_run, ws, payload.get("mode") or "auto",
            {"document_ids": [doc_id], "action": payload.get("action") or "analyze"},
            kind="document_analysis",
        )
    except runner.AgentBusyError as error:
        raise HTTPException(409, detail=str(error)) from error


@router.get("/documents/{doc_id}/analysis-runs/{run_id}")
async def get_document_analysis_run(workspace_id: str, doc_id: str, run_id: str):
    run = store.load_run(_ws(workspace_id), run_id)
    if doc_id not in ((run.get("document_analysis") or {}).get("document_ids") or []):
        raise workspaces.WorkspaceError("This run does not analyze the selected document.")
    return run


@router.patch("/documents/{doc_id}/analysis/review")
async def patch_document_analysis_review(workspace_id: str, doc_id: str, payload: dict = Body(...)):
    return document_analysis.patch_review(_ws(workspace_id), doc_id, payload)


@router.post("/documents/{doc_id}/analysis/accept-candidate")
async def accept_document_analysis_candidate(workspace_id: str, doc_id: str, payload: dict = Body(...)):
    return document_analysis.accept_candidate(_ws(workspace_id), doc_id, payload)


@router.post("/documents/{doc_id}/reindex")
async def reindex_document(workspace_id: str, doc_id: str):
    return await asyncio.to_thread(document_search.reindex_job, _ws(workspace_id), [doc_id])


@router.patch("/documents/{doc_id}")
async def patch_document(workspace_id: str, doc_id: str, payload: dict = Body(...)):
    ws = _ws(workspace_id)
    result = documents.update_document(ws, doc_id, payload)
    runner.notify_evidence_available(ws, document_ids=[doc_id], reason="document_classified")
    return result


@router.delete("/documents/{doc_id}")
async def delete_document(workspace_id: str, doc_id: str):
    ws = _ws(workspace_id); documents.remove_document(ws, doc_id)
    return {"ok": True}


@router.post("/documents/{doc_id}/re-extract")
async def reextract_document(workspace_id: str, doc_id: str):
    ws = _ws(workspace_id)
    extracted = await asyncio.to_thread(documents.extract_document, ws, doc_id, force=True)
    job = document_search.enqueue_indexing(ws, [doc_id], reason="reextract")
    return {**extracted, "indexing_job": job}


@router.get("/documents/{doc_id}/preview")
async def preview_document(workspace_id: str, doc_id: str, pages: str | None = None):
    return documents.preview(_ws(workspace_id), doc_id, _pages(pages))


@router.get("/documents/{doc_id}/file")
async def serve_document(workspace_id: str, doc_id: str):
    ws = _ws(workspace_id)
    doc = next((item for item in ws.documents if item.get("id") == doc_id), None)
    if doc is None:
        raise workspaces.WorkspaceError(f"Document '{doc_id}' not found.")
    return FileResponse(
        documents.document_path(ws, doc),
        filename=doc.get("source") or doc["file"],
        content_disposition_type="inline",
    )


@router.post("/doc-chat")
async def doc_chat(workspace_id: str, payload: dict = Body(...)):
    ws = _ws(workspace_id)
    return documents.document_chat(
        ws, str(payload.get("document_id") or ""), str(payload.get("question") or ""),
        payload.get("pages"), run_id=payload.get("run_id"),
    )


@router.get("/ai-activity")
async def ai_activity(workspace_id: str, cursor: int = 0, limit: int = 100, document_id: str | None = None):
    return documents.activities(_ws(workspace_id), cursor, limit, document_id)


@router.get("/knowledge-packs")
async def list_knowledge_packs(workspace_id: str):
    return {"items": methodology.list_packs(_ws(workspace_id))}


@router.post("/knowledge-packs")
async def upload_knowledge_pack(workspace_id: str, file: UploadFile = File(...), name: str = Form(""), scope: str = Form("workspace")):
    content = (await file.read()).decode("utf-8", errors="replace")
    return methodology.save_pack(_ws(workspace_id), name or file.filename or "Methodology", content, scope=scope, source=file.filename or "")


@router.get("/knowledge-packs/search")
async def search_knowledge_packs(workspace_id: str, q: str = Query(...), limit: int = 10):
    return {"items": methodology.search(_ws(workspace_id), q, limit=limit)}


@router.get("/knowledge-packs/{scope}/{pack_id}")
async def get_knowledge_pack(workspace_id: str, scope: str, pack_id: str):
    return methodology.get_pack(_ws(workspace_id), scope, pack_id)


@router.patch("/knowledge-packs/{scope}/{pack_id}")
async def patch_knowledge_pack(workspace_id: str, scope: str, pack_id: str, payload: dict = Body(...)):
    existing = methodology.get_pack(_ws(workspace_id), scope, pack_id)
    return methodology.save_pack(
        _ws(workspace_id), payload.get("name", existing["name"]), payload.get("markdown", existing["markdown"]),
        scope=scope, source=existing.get("source") or "", pack_id=pack_id,
    )


@router.delete("/knowledge-packs/{scope}/{pack_id}")
async def delete_knowledge_pack(workspace_id: str, scope: str, pack_id: str):
    methodology.remove_pack(_ws(workspace_id), scope, pack_id)
    return {"ok": True}
