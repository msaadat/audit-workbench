"""Document, privacy, AI-activity, and methodology-pack endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Body, File, Form, Query, UploadFile
from fastapi.responses import FileResponse

from .. import documents, intake, methodology, workspaces

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
    return {"items": _ws(workspace_id).documents}


@router.post("/documents")
async def upload_documents(workspace_id: str, files: list[UploadFile] = File(...), category: str = Form("other")):
    ws = _ws(workspace_id)
    added = []
    for file in files:
        added.append(documents.add_document(ws, file.filename or "document", await file.read(), category=category))
    return {
        "added": added,
        "items": ws.documents,
        "suggested_actions": intake.planning_actions_for_documents(added),
    }


@router.get("/documents/disclosures")
async def list_disclosures(workspace_id: str, cursor: int = 0, limit: int = 100):
    return documents.disclosures(_ws(workspace_id), cursor, limit)


@router.get("/documents/{doc_id}")
async def get_document(workspace_id: str, doc_id: str):
    ws = _ws(workspace_id)
    return documents.preview(ws, doc_id, None) | {"versions": documents.versions(ws, doc_id)}


@router.patch("/documents/{doc_id}")
async def patch_document(workspace_id: str, doc_id: str, payload: dict = Body(...)):
    return documents.update_document(_ws(workspace_id), doc_id, payload)


@router.delete("/documents/{doc_id}")
async def delete_document(workspace_id: str, doc_id: str):
    ws = _ws(workspace_id); documents.remove_document(ws, doc_id)
    return {"ok": True}


@router.post("/documents/{doc_id}/re-extract")
async def reextract_document(workspace_id: str, doc_id: str):
    return documents.extract_document(_ws(workspace_id), doc_id, force=True)


@router.get("/documents/{doc_id}/preview")
async def preview_document(workspace_id: str, doc_id: str, pages: str | None = None):
    return documents.preview(_ws(workspace_id), doc_id, _pages(pages))


@router.get("/documents/{doc_id}/file")
async def serve_document(workspace_id: str, doc_id: str):
    ws = _ws(workspace_id)
    doc = next((item for item in ws.documents if item.get("id") == doc_id), None)
    if doc is None:
        raise workspaces.WorkspaceError(f"Document '{doc_id}' not found.")
    return FileResponse(documents.document_path(ws, doc), filename=doc.get("source") or doc["file"])


@router.get("/documents/{doc_id}/versions")
async def document_versions(workspace_id: str, doc_id: str):
    return {"items": documents.versions(_ws(workspace_id), doc_id)}


@router.patch("/settings")
async def patch_workspace_settings(workspace_id: str, payload: dict = Body(...)):
    ws = _ws(workspace_id)
    allowed = {"doc_llm_optin", "doc_pii_masking"}
    if set(payload) - allowed:
        raise workspaces.WorkspaceError("Unknown workspace setting.")
    if "doc_llm_optin" in payload:
        enabled = bool(payload["doc_llm_optin"])
        ws.settings["doc_llm_optin"] = enabled
        ws.settings["doc_llm_optin_at"] = documents.utcnow() if enabled else None
    if "doc_pii_masking" in payload:
        ws.settings["doc_pii_masking"] = bool(payload["doc_pii_masking"])
    ws.save()
    return ws.settings


@router.post("/doc-chat")
async def doc_chat(workspace_id: str, payload: dict = Body(...)):
    ws = _ws(workspace_id)
    return documents.document_chat(
        ws, str(payload.get("document_id") or ""), str(payload.get("question") or ""),
        payload.get("pages"), mask_pii=bool(payload.get("mask_pii", ws.settings.get("doc_pii_masking"))),
        run_id=payload.get("run_id"),
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
