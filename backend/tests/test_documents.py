import io
import json
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import documents, evidence, llm, methodology, workspaces
from app.main import create_app


def _docx(text: str) -> bytes:
    content = io.BytesIO()
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>'
    )
    with zipfile.ZipFile(content, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return content.getvalue()


def test_text_docx_and_image_extraction_cache():
    ws = workspaces.create_workspace("Document extraction")
    text = documents.add_document(ws, "policy.txt", b"Control owners review exceptions every month.")
    extracted = documents.extract_document(ws, text["id"])
    assert extracted["pages"][0]["text"].startswith("Control owners")
    assert extracted["state"] == "extracted"
    assert documents.cache_path(ws, text["id"]).exists()

    docx = documents.add_document(ws, "minutes.docx", _docx("The committee approved the remediation plan."))
    assert "remediation" in documents.preview(ws, docx["id"])["pages"][0]["text"]

    image = documents.add_document(ws, "receipt.png", b"not-decoded-by-text-extraction")
    page = documents.preview(ws, image["id"])["pages"][0]
    assert page["image_only"] is True
    assert image["text_state"] == "image_only"


def test_document_versions_do_not_retarget_typed_anchor():
    ws = workspaces.create_workspace("Document versions")
    first = documents.add_document(ws, "policy.txt", b"Version one policy")
    anchor = evidence.document_anchor(first, 1, "Version one", generated_by="test")
    second = documents.add_document(ws, "policy.txt", b"Version two policy")
    assert second["version"] == 2 and second["supersedes"] == first["id"]
    assert anchor["source_id"] == first["id"]
    assert anchor["source_sha1"] == first["sha1"] != second["sha1"]


def test_disclosure_requires_optin_selects_pages_masks_and_logs():
    ws = workspaces.create_workspace("Disclosure")
    doc = documents.add_document(ws, "contact.txt", b"Contact jane@example.com or +1 202 555 0188.")
    with pytest.raises(documents.DocPrivacyError):
        documents.disclosable_content(ws, doc["id"], "document_qa", pages=[1])

    ws.settings.update(doc_llm_optin=True, doc_llm_optin_at=documents.utcnow())
    ws.save()
    result = documents.disclosable_content(ws, doc["id"], "document_qa", pages=[1], mask_pii=True)
    assert result["pages"][0]["text"] == "Contact [email masked] or [number masked]."
    logged = documents.disclosures(ws)["items"]
    assert logged[0]["pages"] == [1]
    assert logged[0]["source_sha1"] == doc["sha1"]
    assert "jane@example.com" not in json.dumps(logged)
    with pytest.raises(workspaces.WorkspaceError):
        documents.disclosable_content(ws, doc["id"], "document_qa", pages=[])


def test_assistant_document_context_shares_budget_and_reports_trimming():
    ws = workspaces.create_workspace("Assistant document budget")
    first = documents.add_document(ws, "first.txt", b"A" * 120)
    second = documents.add_document(ws, "second.txt", b"B" * 20)
    ws.settings["doc_llm_optin"] = True
    ws.save()

    context = documents.assistant_document_context(
        ws, [first["id"], second["id"], first["id"]], max_characters=60,
    )

    assert [item["document_id"] for item in context["manifest"]] == [first["id"], second["id"]]
    assert sum(item["characters_disclosed"] for item in context["manifest"]) == 60
    assert context["trimmed"] is True
    assert context["manifest"][1]["characters_disclosed"] == 20
    logged = documents.disclosures(ws)["items"]
    assert len(logged) == 2
    assert all(item["purpose"] == "assistant_chat" for item in logged)
    assert all("characters_disclosed" in item for item in logged)


def test_doc_chat_creates_citations_and_content_free_activity(monkeypatch):
    ws = workspaces.create_workspace("Document Q&A")
    doc = documents.add_document(ws, "policy.txt", b"Invoices require approval by the finance director before payment.")
    ws.settings["doc_llm_optin"] = True
    ws.save()
    calls = []

    def fake_chat(messages, **kwargs):
        calls.append(messages)
        return {"content": json.dumps({"answer": "Finance director approval is required.", "citations": [{"page": 1, "excerpt": "Invoices require approval by the finance director before payment."}]})}

    monkeypatch.setattr(llm, "chat", fake_chat)
    monkeypatch.setattr(llm, "agent_status", lambda: {"provider": "fake", "model": "fake", "configured": True})
    result = documents.document_chat(ws, doc["id"], "Who approves invoices?", [1])
    assert result["citations"][0]["source_sha1"] == doc["sha1"]
    assert "finance director" in calls[0][1]["content"]
    activity = documents.activities(ws)["items"][0]
    serialized = json.dumps(activity)
    assert activity["document_ids"] == [doc["id"]]
    assert "finance director" not in serialized


def test_legacy_evidence_hydrates_and_new_writes_are_typed():
    ws = workspaces.create_workspace("Legacy anchors")
    procedure = ws.add_procedure({"objective": "Inspect evidence"})
    ws.work_program[0]["evidence_refs"] = ["analysis:old-result"]
    ws.save()
    reloaded = workspaces.load_workspace(ws.id)
    assert reloaded.work_program[0]["evidence_refs"][0]["legacy_ref"] == "analysis:old-result"
    with pytest.raises(workspaces.WorkspaceError):
        reloaded.update_procedure(procedure["id"], {"evidence_refs": ["analysis:new-result"]})


def test_methodology_pack_versions_and_lexical_citations():
    ws = workspaces.create_workspace("Methodology")
    first = methodology.save_pack(ws, "Internal Audit Guide", "# Sampling\nUse a documented random seed.")
    second = methodology.save_pack(ws, "Internal Audit Guide", "# Sampling\nUse a reproducible documented random seed.")
    assert first["version"] == 1 and second["version"] == 2
    result = methodology.search(ws, "reproducible seed")[0]
    assert result["pack_name"] == "Internal Audit Guide"
    assert result["version"] == 2
    assert result["section"] == "Sampling"


def test_document_privacy_and_knowledge_apis(monkeypatch):
    client = TestClient(create_app())
    workspace_id = client.post("/api/workspaces", json={"name": "Document API"}).json()["id"]
    base = f"/api/workspaces/{workspace_id}"
    upload = client.post(f"{base}/documents", files={"files": ("policy.txt", b"Policy evidence", "text/plain")})
    assert upload.status_code == 200
    assert upload.json()["suggested_actions"][0]["agent_kind"] == "planning"
    doc = upload.json()["added"][0]
    assert client.get(f"{base}/documents/{doc['id']}/preview").json()["pages"][0]["text"] == "Policy evidence"
    assert client.post(f"{base}/doc-chat", json={"document_id": doc["id"], "question": "What?", "pages": [1]}).status_code == 400
    settings = client.patch(f"{base}/settings", json={"doc_llm_optin": True}).json()
    assert settings["doc_llm_optin_at"]
    pack = client.post(
        f"{base}/knowledge-packs",
        data={"name": "Firm Guide", "scope": "workspace"},
        files={"file": ("guide.md", b"# Controls\nReview access quarterly.", "text/markdown")},
    )
    assert pack.status_code == 200
    assert client.get(f"{base}/knowledge-packs/search", params={"q": "quarterly access"}).json()["items"][0]["citation"].startswith("Firm Guide")


def test_prompt_modules_do_not_read_extraction_cache_directly():
    root = Path(__file__).resolve().parents[1] / "app"
    paths = [root / "agent" / "prompts.py", root / "assistant.py"]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert ".extracted" not in text
        assert "cache_path(" not in text
