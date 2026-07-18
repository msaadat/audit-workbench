import json
import time

import pytest
from fastapi.testclient import TestClient

from app import document_analysis, document_context, document_search, documents, embedding, llm, workspaces
from app.agent import runner, store
from app.main import create_app


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


def test_durable_document_analysis_run_persists_valid_citations(monkeypatch):
    ws = workspaces.create_workspace("Durable document analysis")
    source = "Invoices require finance director approval before payment."
    doc = documents.add_document(ws, "policy.txt", source.encode())

    def fake_chat(messages, **_kwargs):
        tag = messages[0]["content"].split("]", 1)[0].lstrip("[")
        assert tag == "agent:document_analysis_map"
        return {"content": json.dumps({
            "summary_markdown": "Invoices require approval. [1]",
            "audit_notes_markdown": "The policy states a requirement; operation is not demonstrated.",
            "citations": [{"id": "1", "page": 1, "excerpt": source}],
        })}

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(llm, "agent_status", lambda: {"configured": True, "provider": "local", "model": "test"})
    run = runner.start_run(ws, "auto", {"document_ids": [doc["id"]], "action": "analyze"}, kind="document_analysis")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        finished = store.load_run(ws, run["id"])
        if finished["status"] in store.TERMINAL_STATUSES:
            break
        time.sleep(.01)
    assert finished["status"] == "completed"
    analysis = document_analysis.load_analysis(ws, doc["id"])
    assert analysis["effective"]["citations"][0]["excerpt_hash"]
    assert analysis["status"]["analysis_coverage_state"] == "complete"


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
