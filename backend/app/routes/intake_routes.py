"""Browser folder-source and durable incremental-import endpoints."""

from __future__ import annotations

import hashlib

from fastapi import APIRouter, Body, File, Form, UploadFile

from .. import intake, uploads, workspaces
from ..agent import runner

router = APIRouter(prefix="/api/workspaces", tags=["folder-intake"])


@router.get("/{workspace_id}/folder-sources")
def list_folder_sources(workspace_id: str):
    ws = workspaces.load_workspace(workspace_id)
    return {"sources": intake.list_sources(ws)}


@router.post("/{workspace_id}/folder-sources")
def create_folder_source(workspace_id: str, payload: dict = Body(...)):
    ws = workspaces.load_workspace(workspace_id)
    return intake.create_source(
        ws, payload.get("label") or "", payload.get("root_name") or ""
    )


@router.post("/{workspace_id}/folder-sources/{source_id}/imports")
def compare_folder_import(
    workspace_id: str, source_id: str, payload: dict = Body(...)
):
    ws = workspaces.load_workspace(workspace_id)
    return intake.compare_manifest(
        ws,
        source_id,
        payload.get("manifest") or [],
        payload.get("mode") or "permission",
        bool(payload.get("force")),
    )


@router.post("/{workspace_id}/folder-imports")
def start_folder_import(workspace_id: str, payload: dict = Body(...)):
    """Start an import without exposing internal folder-source bookkeeping."""
    ws = workspaces.load_workspace(workspace_id)
    manifest = payload.get("manifest") or []
    source = intake.resolve_source(ws, payload.get("root_name") or "", manifest)
    return intake.compare_manifest(
        ws,
        source["id"],
        manifest,
        payload.get("mode") or "permission",
        bool(payload.get("force")),
    )


@router.post("/{workspace_id}/folder-imports/{batch_id}/files")
async def upload_folder_file(
    workspace_id: str,
    batch_id: str,
    relative_path: str = Form(...),
    file: UploadFile = File(...),
):
    ws = workspaces.load_workspace(workspace_id)
    batch = intake.load_batch(ws, batch_id)
    if batch.get("status") != "uploading":
        raise workspaces.WorkspaceError("This import batch is no longer accepting uploads.")
    item = intake.requested_item(batch, relative_path)
    target = intake.staging_path(ws, batch, item)
    target.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1()
    size = 0
    cap = uploads.max_upload_bytes()
    try:
        with target.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > cap:
                    raise workspaces.WorkspaceError(
                        f"'{relative_path}' is larger than the "
                        f"{uploads.describe_bytes(cap)} upload limit."
                    )
                digest.update(chunk)
                handle.write(chunk)
        uploads.check_quota(ws.owner_id, size)
        intake.mark_uploaded(ws, batch, item, digest.hexdigest(), size, target)
    except Exception:
        if not item.get("uploaded"):
            target.unlink(missing_ok=True)
        raise
    return {"item_id": item["id"], "sha1": item["sha1"], "size": size}


@router.post("/{workspace_id}/folder-imports/{batch_id}/complete-upload")
def complete_folder_upload(workspace_id: str, batch_id: str):
    ws = workspaces.load_workspace(workspace_id)
    return intake.complete_upload(ws, batch_id)


@router.post("/{workspace_id}/folder-imports/{batch_id}/apply")
def apply_folder_import(workspace_id: str, batch_id: str, payload: dict = Body(default={})):
    """Apply the batch from deterministic classifications plus user edits.

    This is the assistant-free path: no agent run or model call is involved.
    """
    ws = workspaces.load_workspace(workspace_id)
    before = {str(item.get("id")) for item in ws.documents}
    result = intake.apply_batch(ws, batch_id, (payload or {}).get("decisions") or [])
    current = workspaces.load_workspace(workspace_id)
    added = [str(item.get("id")) for item in current.documents if str(item.get("id")) not in before]
    if added:
        runner.notify_evidence_available(current, document_ids=added, reason="folder_import_applied")
    return result


@router.get("/{workspace_id}/folder-imports/{batch_id}")
def get_folder_import(workspace_id: str, batch_id: str):
    ws = workspaces.load_workspace(workspace_id)
    return intake.load_batch(ws, batch_id)


@router.delete("/{workspace_id}/folder-imports/{batch_id}")
def cancel_folder_import(workspace_id: str, batch_id: str):
    ws = workspaces.load_workspace(workspace_id)
    intake.cancel_batch(ws, batch_id)
    return {"ok": True}
