import json
import time

import pytest
from fastapi.testclient import TestClient

from app import document_analysis, document_context, document_search, documents, embedding, llm, workspaces
from app.agent import prompts, runner, store
from app.agent.context import adapters
from app.main import create_app
from conftest import wait_run


def test_local_hybrid_index_resolves_exact_page_text_without_model(monkeypatch):
    ws = workspaces.create_workspace("Local document search")
    first = documents.add_document(
        ws, "policy.txt",
        b"Section PR-204 requires dual approval for purchases above USD 25,000.\n\nEmergency purchases follow section PR-310.",
        category="policy",
    )
    second = documents.add_document(ws, "minutes.txt", b"The committee discussed office repairs.")

    manifest = document_search.build_index(ws, first["id"])
    document_search.build_index(ws, second["id"])
    result = document_search.search(ws, "PR-204 USD 25,000", top_k=3)

    assert manifest["state"] == "ready"
    assert result["results"][0]["document_id"] == first["id"]
    assert "dual approval" in result["results"][0]["excerpt"]
    assert result["results"][0]["citation"]["source_sha1"] == first["sha1"]
    assert embedding.status()["dimension"] == 384


def test_lexical_search_survives_missing_embedding_runtime():
    ws = workspaces.create_workspace("Lexical document search")
    doc = documents.add_document(ws, "policy.txt", b"Control code ZX-991 requires a quarterly certification.")
    original = embedding.get_backend()
    try:
        embedding.set_backend(None, "model unavailable")
        manifest = document_search.build_index(ws, doc["id"])
        result = document_search.search(ws, "ZX-991 certification", document_ids=[doc["id"]])
    finally:
        embedding.set_backend(original)
    assert manifest["lexical_only"] is True
    assert result["results"][0]["page"] == 1


def test_image_only_visual_transcription_is_indexed_locally_with_origin():
    ws = workspaces.create_workspace("Visual transcription search")
    doc = documents.add_document(ws, "org-chart.png", b"not-readable-as-an-image")
    extracted = documents.extract_document(ws, doc["id"])
    document_analysis.persist_analysis(
        ws,
        doc,
        extracted,
        {
            "derived_text_markdown": (
                "The procurement manager reports to the chief financial officer."
            ),
            "summary_markdown": "Organization reporting lines.",
            "audit_notes_markdown": "Confirm against the approved organization register.",
            "citations": [
                {
                    "id": "V1",
                    "page": 1,
                    "evidence_kind": "visual",
                    "description": "Procurement manager below CFO.",
                    "source_sha1": doc["sha1"],
                }
            ],
            "vision_used": True,
            "prepared_media_set_hash": f"sha256:{'4' * 64}",
        },
        provider="local",
        model="vision-test",
        coverage={
            "state": "complete",
            "analyzed_pages": [1],
            "text_analyzed_pages": [],
            "vision_analyzed_pages": [1],
            "omitted_pages": [],
            "omissions": [],
        },
    )
    original = embedding.get_backend()
    try:
        embedding.set_backend(None, "test lexical path")
        manifest = document_search.build_index(ws, doc["id"])
        result = document_search.search(
            ws,
            "procurement manager chief financial officer",
            document_ids=[doc["id"]],
        )
    finally:
        embedding.set_backend(original)

    assert manifest["state"] == "ready"
    assert result["results"][0]["origin"] == "vision_transcript"
    assert result["results"][0]["citation"]["evidence_kind"] == (
        "model_generated_transcription"
    )
    assert result["results"][0]["citation"]["auditor_confirmed"] is False


def test_local_vector_signal_handles_audit_concept_synonyms():
    ws = workspaces.create_workspace("Semantic document search")
    relevant = documents.add_document(ws, "procurement.txt", b"Every purchase must receive approval before commitment.")
    documents.add_document(ws, "facilities.txt", b"The office lease describes parking and maintenance.")
    result = document_search.search(ws, "procurement authorization requirement", top_k=2)
    assert result["results"][0]["document_id"] == relevant["id"]
    assert result["results"][0]["vector_score"] > 0


def test_analysis_chunks_are_complete_and_non_overlapping():
    text = "A" * 120 + "\n\n" + "B" * 120
    chunks = document_analysis.analysis_chunks({"pages": [{"page": 1, "text": text}]}, max_characters=100)
    assert "".join(chunk["text"] for chunk in chunks) == text
    assert [(chunk["start_character"], chunk["end_character"]) for chunk in chunks] == [
        (chunks[0]["start_character"], chunks[0]["end_character"]),
        *[(chunks[index - 1]["end_character"], chunks[index]["end_character"]) for index in range(1, len(chunks))],
    ]


def test_document_analysis_metadata_excludes_internal_storage_paths():
    # The map worker owns this projection now (Phase 9). Metadata is a fallible
    # classification hint, never citation evidence, and it never carries the
    # internal storage filename.
    from app.agent.workers import documents as document_workers

    metadata = document_workers.document_metadata(
        {
            "id": "doc-1", "file": "doc-1.docx", "source": "Procurement SOP.docx",
            "relative_path": "Planning/Policies/Procurement SOP.docx",
            "title": "procurement_sop", "category": "policy",
            "note": "Current extract", "sha1": "abc123",
        }
    )

    assert metadata == {
        "document_id": "doc-1",
        "title": "procurement_sop",
        "original_filename": "Procurement SOP.docx",
        "category": "policy",
        "folder_context": "Planning/Policies",
        "user_note": "Current extract",
    }
    assert "doc-1.docx" not in json.dumps(metadata)


def test_analysis_overrides_candidate_and_replacement_staleness():
    ws = workspaces.create_workspace("Persistent document analysis")
    doc = documents.add_document(ws, "policy.txt", b"Approvals are required before payment.")
    extracted = documents.extract_document(ws, doc["id"])
    first = document_analysis.persist_analysis(
        ws, doc, extracted,
        {"summary_markdown": "Generated summary", "audit_notes_markdown": "Generated notes", "citations": []},
        provider="local", model="test",
    )
    edited = document_analysis.patch_review(ws, doc["id"], {
        "review_revision": first["review_revision"], "summary_markdown": "Auditor summary", "review_state": "reviewed",
    })
    candidate = document_analysis.persist_analysis(
        ws, doc, extracted,
        {"summary_markdown": "Candidate summary", "audit_notes_markdown": "Candidate notes", "citations": []},
        provider="local", model="new", action="refresh",
    )

    assert candidate["effective"]["summary_markdown"] == "Auditor summary"
    assert candidate["candidate"]["summary_markdown"] == "Candidate summary"
    with pytest.raises(workspaces.WorkspaceError, match="reload"):
        document_analysis.patch_review(ws, doc["id"], {"review_revision": first["review_revision"], "summary_markdown": "lost update"})

    accepted = document_analysis.accept_candidate(ws, doc["id"], {
        "index_revision": candidate["index_revision"], "review_revision": candidate["review_revision"],
    })
    assert accepted["generated"]["summary_markdown"] == "Candidate summary"
    assert accepted["effective"]["summary_markdown"] == "Auditor summary"

    replaced = documents.add_document(ws, "policy.txt", b"A different replacement policy.", replace=True)
    stale = document_analysis.load_analysis(ws, doc["id"], document=replaced)
    assert stale["status"]["analysis_validity_state"] == "stale"
    assert document_analysis.compact_artifact(ws, doc["id"]) is None

    context = document_context.apm_document_context(ws, doc["id"])
    assert context["outcome"] == "supplied"
    assert "Auditor summary" in context["content"]
    assert context["analysis_validity_state"] == "stale"
    candidate = adapters.apm_document_candidates(ws)[0]
    assert candidate.representations["summary"] == context["content"]
    assert candidate.metadata["analysis_validity_state"] == "stale"


def test_broker_refuses_unscoped_large_attachment_without_prefix():
    ws = workspaces.create_workspace("Attachment scope")
    doc = documents.add_document(ws, "large.txt", b"X" * 40_000)
    result = document_context.get_document_context(
        ws, doc["id"], "full", max_characters=32_000, record_activity=False,
    )
    attached = document_context.assistant_attachments(ws, [doc["id"]], max_characters=80_000)

    assert result["outcome"] == "scope_required"
    assert result["content"] == ""
    assert attached["documents"] == []
    assert attached["manifest"][0]["characters_included"] == 0
    assert attached["scope_required"] is True


def _document_workflow_run(ws, document_ids, *, action="analyze"):
    """Start the declared document workflow the way the Documents tab does."""

    from app.agent.workflows import documents as documents_workflow

    return runner.start_command_run(
        ws,
        "auto",
        {
            "source": "tab_button",
            "text": f"Analyze {len(document_ids)} selected document(s).",
            "goal_template": "document_analysis",
            "requested_outcomes": list(documents_workflow.FULL_DOCUMENT_OUTCOMES),
            "target_refs": [f"document:{value}" for value in document_ids],
            "generation_mode": "force" if action == "refresh" else "reuse_existing",
        },
        context={"document_ids": list(document_ids), "action": action},
    )


# Every document run names what each document *is* before mapping it. These
# hand-rolled fakes dispatch on the prompt tag rather than asserting a single
# one, so the classification pass is answered and the assertion that follows
# still pins the map call it cares about.
CLASSIFY_TAG = "agent:document_classification"
CLASSIFICATION_REPLY = {
    "document_type": "other",
    "document_type_other": "Test fixture document",
    "confidence": "low",
    "rationale": "A fixture document with no catalogued form.",
}

def test_durable_document_analysis_run_persists_valid_citations(monkeypatch):
    ws = workspaces.create_workspace("Durable document analysis")
    source = "Invoices require finance director approval before payment."
    doc = documents.add_document(ws, "policy.txt", source.encode())

    def fake_chat(messages, **_kwargs):
        tag = messages[0]["content"].split("]", 1)[0].lstrip("[")
        if tag == CLASSIFY_TAG:
            return {"content": json.dumps(CLASSIFICATION_REPLY)}
        assert tag == "agent:document_analysis_map"
        payload = {
            "summary_markdown": "Invoices require approval. [1]",
            "audit_notes_markdown": "The policy states a requirement; operation is not demonstrated.",
            "citations": [{"id": "1", "page": 1, "excerpt": source}],
        }
        return {"content": json.dumps(payload)}

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(llm, "agent_status", lambda: {"configured": True, "provider": "local", "model": "test"})
    finished = wait_run(ws, _document_workflow_run(ws, [doc["id"]])["id"])

    assert finished["status"] == "completed"
    analysis = document_analysis.load_analysis(ws, doc["id"])
    assert analysis["effective"]["citations"][0]["excerpt_hash"]
    assert analysis["status"]["analysis_coverage_state"] == "complete"
    # The artifact carries the workflow provenance an interrupted commit is
    # reconciled against.
    assert analysis["generated"]["agent_run_id"] == finished["id"]
    assert analysis["generated"]["content_sha1"]


def test_durable_document_analysis_persists_freeform_long_markdown(monkeypatch):
    ws = workspaces.create_workspace("Freeform document narrative")
    source = "Invoices require finance director approval before payment."
    doc = documents.add_document(ws, "policy.txt", source.encode(), category="policy")
    paragraph = " ".join(
        [
            "The supplied policy describes the approval requirement and its place "
            "in the payment process. [C1]"
        ]
        * 7
    )
    summary = (
        "## Purpose and applicability\n\n"
        + paragraph
        + "\n\n## Approval requirements\n\n"
        + paragraph
        + "\n\n- Finance director approval precedes payment. [C1]"
    )
    notes = (
        "## Audit notes\n\n### Operating evidence\n\n"
        "The supplied text states a requirement only. [C1]\n\n"
        "**Why it matters:** Operation requires separate evidence. [C1]\n\n"
        "**Follow-up:** Obtain approved transactions for testing. [C1]"
    )

    def fake_chat(messages, **kwargs):
        tag = messages[0]["content"].split("]", 1)[0].lstrip("[")
        if tag == CLASSIFY_TAG:
            return {"content": json.dumps(CLASSIFICATION_REPLY)}
        assert tag == "agent:document_analysis_map"
        assert "tools" not in kwargs
        payload = {
            "summary_markdown": summary,
            "audit_notes_markdown": notes,
            "citations": [{"id": "C1", "page": 1, "excerpt": source}],
        }
        return {"content": json.dumps(payload)}

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": True, "provider": "local", "model": "test"},
    )
    finished = wait_run(ws, _document_workflow_run(ws, [doc["id"]])["id"])

    assert finished["status"] == "completed"
    analysis = document_analysis.load_analysis(ws, doc["id"])
    summary = analysis["effective"]["summary_markdown"]
    notes = analysis["effective"]["audit_notes_markdown"]
    assert len(summary) > 1024
    assert summary.startswith("## Purpose and applicability\n\n")
    assert "\n\n## Approval requirements\n\n" in summary
    assert notes.startswith("## Audit notes\n\n### Operating evidence\n\n")


def test_document_analysis_retries_blank_notes_and_persists_complete_output(monkeypatch):
    ws = workspaces.create_workspace("Document analysis retry")
    source = "Invoices require finance director approval before payment."
    doc = documents.add_document(ws, "policy.txt", source.encode(), category="policy")
    calls = 0

    def fake_chat(messages, **_kwargs):
        nonlocal calls
        tag = messages[0]["content"].split("]", 1)[0].lstrip("[")
        if tag == CLASSIFY_TAG:
            # Not counted: the retry this test pins is the map worker's, and
            # counting the classification pass would shift every branch below.
            return {"content": json.dumps(CLASSIFICATION_REPLY)}
        calls += 1
        if calls == 1:
            payload = {
                "summary_markdown": "Invoices require approval.",
                "audit_notes_markdown": "",
                "citations": [{"id": "C1", "page": 1, "excerpt": source}],
            }
        else:
            assert "audit_notes_markdown" in messages[-1]["content"]
            payload = {
                "summary_markdown": "Invoices require approval. [C1]",
                "audit_notes_markdown": "Obtain evidence that the approval operated. [C1]",
                "citations": [{"id": "C1", "page": 1, "excerpt": source}],
            }
        return {"content": json.dumps(payload)}

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(
        llm, "agent_status",
        lambda: {"configured": True, "provider": "local", "model": "test"},
    )
    finished = wait_run(ws, _document_workflow_run(ws, [doc["id"]])["id"])

    assert finished["status"] == "completed"
    assert calls == 2
    analysis = document_analysis.load_analysis(ws, doc["id"])
    assert analysis["effective"]["audit_notes_markdown"].startswith("Obtain evidence")


def test_document_inventory_and_search_api():
    client = TestClient(create_app())
    workspace_id = client.post("/api/workspaces", json={"name": "Search API"}).json()["id"]
    base = f"/api/workspaces/{workspace_id}"
    uploaded = client.post(
        f"{base}/documents", files={"files": ("policy.txt", b"Clause AP-77 requires quarterly review.", "text/plain")},
    ).json()
    document = uploaded["added"][0]
    assert uploaded["indexing_job"]["document_ids"] == [document["id"]]

    inventory = client.get(f"{base}/documents").json()["items"][0]
    assert inventory["analysis_coverage_state"] == "none"
    assert inventory["search_index_state"] in {"indexing", "ready"}
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        status = client.get(f"{base}/documents/indexing-status").json()
        if status["state"] == "idle":
            break
        time.sleep(.01)
    assert status["state"] == "idle"
    result = client.post(f"{base}/documents/search", json={"query": "AP-77", "document_ids": [document["id"]]}).json()
    assert result["results"][0]["page"] == 1
    assert client.get(f"{base}/documents/search-status").json()["documents"][0]["state"] == "ready"
    assert client.get(f"{base}/documents/{document['id']}/analysis").json()["effective"] is None
