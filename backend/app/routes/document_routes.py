"""Document, AI-activity, and methodology-pack endpoints."""

from __future__ import annotations

import asyncio
import hashlib

from fastapi import APIRouter, Body, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

from .. import (
    cycle_linking,
    document_analysis,
    document_classification,
    document_masters,
    document_schemas,
    document_search,
    document_types,
    documents,
    embedding,
    intake,
    methodology,
    workspaces,
)
# Aliased: this module already binds the name ``uploads`` to a request's files.
from .. import uploads as upload_limits
from ..text import counted, plural_word
from ..agent import runner, store
from ..agent.capabilities import documents as document_capabilities
from ..agent.workflows import documents as documents_workflow

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
    # Unset by default. An uploader that knows what a document is may still
    # say so, and that answer stands; anything else is read from page one.
    category: str = Form(""),
    replace: bool = Form(False),
):
    ws = _ws(workspace_id)
    incoming = [
        (file.filename or "document", await upload_limits.read_upload(file))
        for file in files
    ]
    upload_limits.check_quota(ws.owner_id, sum(len(body) for _, body in incoming))
    uploads = incoming
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
            f"{plural_word(len(names.split(', ')), 'Document')} already exist: {names}. "
            "Confirm replacement to overwrite them."
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


#: What the Documents tab may ask for. ``analyze`` fills gaps, ``refresh``
#: re-reads named documents under a frozen vocabulary, and ``revise_vocabulary``
#: rebuilds one from a whole type. See :func:`_analysis_command`.
DOCUMENT_ANALYSIS_ACTIONS = ("analyze", "refresh", "revise_vocabulary")


def _analysis_command(
    ws,
    document_ids: list[str],
    action: str,
    mode: str,
    full_visual_coverage: bool = False,
) -> dict:
    """Start the declared document-analysis workflow for the selected documents.

    Standalone analysis and the audit-planning dependency request the same
    outcome through the same scheduler: this endpoint names the documents as
    workflow targets rather than driving a separate runner.

    ``force`` has to split, because under an accumulating master one button is
    being asked two different questions and can only answer one of them. The old
    compromise — scope forced re-derivation to the targeted documents' own types
    — rested on a schema coming from a *sample*, which made re-deriving one type
    a couple of documents' work. A master comes from every document of the type,
    in order, so "re-read this document" and "possibly move the vocabulary" are
    no longer separable.

    So each question gets its own action, and the expensive one is only ever
    reached deliberately:

    ``refresh``            re-read the targeted documents under the vocabulary
                           their siblings were read under. The master is frozen;
                           a field the read wants to add is *reported*, not
                           applied, and the report names the action that would
                           take it. Cheap by construction.
    ``revise_vocabulary``  re-read every document of the targeted documents'
                           types, rebuilding the master from the pass. This is
                           what re-reading eighteen payment instructions to fix
                           one document's vocabulary actually costs, and naming
                           it is the honesty the split is for.
    """
    document_ids = [str(value) for value in document_ids or []]
    if not document_ids:
        raise workspaces.WorkspaceError("Select at least one document to analyze.")
    known = {str(item.get("id")) for item in ws.documents}
    missing = [value for value in document_ids if value not in known]
    if missing:
        raise workspaces.WorkspaceError(f"Document '{missing[0]}' not found.")
    if action not in DOCUMENT_ANALYSIS_ACTIONS:
        raise workspaces.WorkspaceError(
            "Document analysis action must be analyze, refresh, or "
            "revise_vocabulary."
        )
    return runner.start_command_run(
        ws,
        mode if mode in {"auto", "permission"} else "auto",
        {
            "source": "tab_button",
            "text": f"Analyse {counted(len(document_ids), 'selected document')}.",
            "goal_template": "document_analysis",
            "requested_outcomes": list(
                documents_workflow.FULL_DOCUMENT_OUTCOMES
            ),
            "target_refs": [f"document:{value}" for value in document_ids],
            "generation_mode": (
                "reuse_existing" if action == "analyze" else "force"
            ),
        },
        context={
            "document_ids": document_ids,
            "action": action,
            "full_visual_document_ids": (
                document_ids if full_visual_coverage else []
            ),
        },
    )


@router.post("/documents/analysis-runs")
async def create_document_analysis_run(workspace_id: str, payload: dict = Body(...)):
    ws = _ws(workspace_id)
    try:
        return await asyncio.to_thread(
            _analysis_command,
            ws,
            payload.get("document_ids") or [],
            str(payload.get("action") or "analyze"),
            str(payload.get("mode") or "auto"),
            bool(payload.get("full_visual_coverage")),
        )
    except runner.AgentBusyError as error:
        raise HTTPException(409, detail=str(error)) from error


def _reclassify_command(ws, mode: str) -> dict:
    """Re-examine the ``other`` bucket against the workspace's current catalog.

    Only documents whose stored catalog differs from today's are in scope, which
    is the same rule unit expansion uses — coining a type is what makes an
    ``other`` worth re-asking, and nothing else does. With no such document the
    caller is told so rather than a run being started that would do nothing.
    """

    document_ids = document_classification.reclassifiable_ids(ws)
    if not document_ids:
        raise workspaces.WorkspaceError(
            "No document needs re-examining: every 'other' was already chosen "
            "from the current list of types."
        )
    return runner.start_command_run(
        ws,
        mode if mode in {"auto", "permission"} else "auto",
        {
            "source": "tab_button",
            "text": f"Re-examine {counted(len(document_ids), 'unidentified document')}.",
            "goal_template": "document_analysis",
            # Only the classification outcome. Re-examining what a document is
            # must not re-run its analysis, which is unaffected by the catalog.
            "requested_outcomes": ["documents.types_classified"],
            "target_refs": [f"document:{value}" for value in document_ids],
            "generation_mode": "reuse_existing",
        },
        context={"document_ids": document_ids, "action": "analyze"},
    )


@router.get("/documents/types")
async def list_document_types(workspace_id: str):
    """The catalog a classification may choose from, plus what has been assigned."""

    ws = _ws(workspace_id)
    return {
        **document_types.metadata(),
        "local_types": document_schemas.local_types(ws),
        "summary": document_classification.summary(ws),
    }


@router.get("/documents/schemas")
async def list_document_schemas(workspace_id: str):
    """The induced schemas, and the fields a requirement may be written against.

    The catalog form rather than the stored form: an RCM attribute addresses a
    type and a field, and nothing about how the schema was induced changes what
    it can be asked.
    """

    ws = _ws(workspace_id)
    return {"items": cycle_linking.schema_catalog(ws)}


@router.get("/documents/vocabulary")
async def list_document_vocabulary(workspace_id: str):
    """What each type is read under, and how well corroborated it is.

    The per-type surface the documents tab never had. ``escape_rate`` was meant
    to be it and was never served or rendered; under an accumulating master the
    question it answered — is this vocabulary representative — is answered
    better by the fill counts the master already carries.

    ``types_with_unread_documents`` is the other half: a type is stamped only
    when every document of it has been read, so a type missing one is not a
    vocabulary in progress, it is a vocabulary withheld, and the reason belongs
    on the same screen as the result.
    """

    ws = _ws(workspace_id)
    scope = {"document_scope_mode": "all"}
    return {
        "items": [
            {
                **entry,
                "unread_documents": document_capabilities.unread_documents_of_type(
                    ws, entry["document_type"], scope
                ),
                "schema": document_schemas.load_schema(ws, entry["document_type"]),
            }
            for entry in document_masters.catalog(ws)
        ]
    }


@router.get("/documents/unidentified")
async def list_unidentified_documents(workspace_id: str):
    """The ``other`` bucket an auditor retypes from."""

    ws = _ws(workspace_id)
    return {
        "items": document_classification.other_bucket(ws),
        "reclassifiable": document_classification.reclassifiable_ids(ws),
    }


@router.get("/documents/classifications")
async def list_document_classifications(workspace_id: str):
    """Every assignment, so a wrong one can be corrected and not only a missing one.

    Wider than :func:`list_unidentified_documents` on purpose: ``other`` is the
    bucket a document falls into when the model *knew* nothing fitted, and the
    label a model was confident and wrong about never lands there.
    """

    ws = _ws(workspace_id)
    return {
        "items": document_classification.assignments(ws),
        "reclassifiable": document_classification.reclassifiable_ids(ws),
    }


@router.post("/documents/reclassify")
async def reclassify_documents(workspace_id: str, payload: dict = Body(default={})):
    ws = _ws(workspace_id)
    try:
        return await asyncio.to_thread(
            _reclassify_command, ws, str(payload.get("mode") or "auto")
        )
    except runner.AgentBusyError as error:
        raise HTTPException(409, detail=str(error)) from error


@router.patch("/documents/{doc_id}/type")
async def retype_document(workspace_id: str, doc_id: str, payload: dict = Body(...)):
    """Assign a document type by hand.

    ``type_id`` names an existing entry; ``coin`` names a new one for this
    engagement. Coining registers the type first, so the sweep that follows can
    put the rest of the bucket onto it — retyping one document and leaving forty
    like it unidentified would starve schema induction, which needs several
    documents of a type.
    """

    ws = _ws(workspace_id)
    type_id = str(payload.get("type_id") or "").strip() or None
    coin = str(payload.get("coin") or "").strip() or None
    record = document_classification.retype(
        ws,
        doc_id,
        type_id=type_id,
        coin=coin,
        rationale=str(payload.get("rationale") or ""),
        discriminator=str(payload.get("discriminator") or ""),
    )
    runner.notify_evidence_available(
        ws, document_ids=[doc_id], reason="document_classified"
    )
    return {
        "document_id": doc_id,
        "classification": record,
        # What retyping opened up: the coined type is now on offer, so any
        # ``other`` chosen from the older list is worth re-asking.
        "reclassifiable": document_classification.reclassifiable_ids(ws),
        "summary": document_classification.summary(ws),
    }


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
            _analysis_command,
            ws,
            [doc_id],
            str(payload.get("action") or "analyze"),
            str(payload.get("mode") or "auto"),
            bool(payload.get("full_visual_coverage")),
        )
    except runner.AgentBusyError as error:
        raise HTTPException(409, detail=str(error)) from error


@router.get("/documents/{doc_id}/analysis-runs/{run_id}")
async def get_document_analysis_run(workspace_id: str, doc_id: str, run_id: str):
    run = store.load_run(_ws(workspace_id), run_id)
    scoped = {
        *((run.get("document_analysis") or {}).get("document_ids") or []),
        *(
            str(ref).split(":", 1)[1]
            for ref in ((run.get("workflow") or {}).get("target_refs") or [])
            if str(ref).startswith("document:")
        ),
    }
    if doc_id not in scoped:
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
    body = await upload_limits.read_upload(file)
    upload_limits.check_quota(_ws(workspace_id).owner_id, len(body))
    content = body.decode("utf-8", errors="replace")
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
