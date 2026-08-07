import hashlib
import time

from fastapi.testclient import TestClient

from app import intake, workspaces
from app.main import create_app
from app.agent import runner, store


def _workspace():
    return workspaces.create_workspace("Folder Intake")


def _source_and_batch(ws, manifest, mode="permission"):
    source = intake.create_source(ws, "Audit folder", "Client Audit")
    result = intake.compare_manifest(ws, source["id"], manifest, mode)
    return source, result["batch"]


def _stage(ws, batch, relative_path, content):
    item = intake.requested_item(batch, relative_path)
    target = intake.staging_path(ws, batch, item)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    intake.mark_uploaded(
        ws, batch, item, hashlib.sha1(content).hexdigest(), len(content), target
    )


def test_deterministic_document_categories_use_safe_filename_metadata():
    def classify(path):
        return intake.deterministic_classification({
            "relative_path": path,
            "local_metadata": {"route": "document", "parse_ok": True},
        })["document_category"]

    assert classify("Planning/Procurement SOP Extracts.docx") == "policy"
    assert classify("Planning/Minutes of Meeting - CFO.docx") == "minutes"
    assert classify("Planning/Email from Senior.docx") == "correspondence"
    assert classify("Samples/INV001_Signed_Payment_Voucher.pdf") == "voucher"
    for filename in (
        "INV2024004_Invoice.pdf",
        "PO2024004_Purchase_Order.pdf",
        "REQ2024009_Purchase_Requisition.pdf",
        "GRN2024004_Signed_Receipt.pdf",
        "Supplier_Quotation_1042.pdf",
    ):
        assert classify(f"Samples/Transaction-104/{filename}") == "voucher"

    # A broad "approval" match used to classify ordinary planning records as
    # transaction evidence. Transaction classification now relies on a
    # document signal in the filename instead.
    assert classify("Planning/Financial_Approval_Matrix.docx") == "policy"
    assert classify("Planning/March_Approval_Memo.pdf") == "other"


def test_model_cannot_replace_high_confidence_transaction_category():
    item = {
        "id": "invoice",
        "relative_path": "Samples/INV2024004_Invoice.pdf",
        "local_metadata": {"route": "document", "parse_ok": True},
    }
    item["classification"] = intake.deterministic_classification(item)
    batch = {"items": [item]}

    intake.merge_model_classifications(batch, [{
        "id": "invoice",
        "route": "document",
        "document_category": "evidence",
        "confidence": "high",
    }])

    assert item["classification"]["document_category"] == "voucher"


def test_manifest_compare_is_incremental_and_reports_exclusions():
    ws = _workspace()
    source, first = _source_and_batch(
        ws,
        [
            {"relative_path": "Audit/ledger.csv", "size": 8, "last_modified": 10},
            {"relative_path": "Audit/~$locked.xlsx", "size": 20, "last_modified": 10},
            {"relative_path": "Audit/readme.exe", "size": 4, "last_modified": 10},
        ],
    )
    assert [item["relative_path"] for item in first["items"] if item["needs_upload"]] == [
        "Audit/ledger.csv"
    ]
    assert first["excluded_count"] == 1
    assert first["unsupported_count"] == 1

    index = intake.load_index(ws)
    index["files"].append(
        {
            "source_id": source["id"],
            "relative_path": "Audit/ledger.csv",
            "size": 8,
            "last_modified": 10,
            "sha1": "old",
        }
    )
    intake.save_index(ws, index)
    second = intake.compare_manifest(
        ws,
        source["id"],
        [{"relative_path": "Audit/ledger.csv", "size": 8, "last_modified": 10}],
    )["batch"]
    assert second["unchanged_count"] == 1
    assert second["items"][0]["needs_upload"] is False


def test_folder_source_is_created_and_resolved_from_manifest_overlap():
    ws = _workspace()
    manifest = [
        {"relative_path": "Client Audit/ledger.csv", "size": 8, "last_modified": 10},
        {"relative_path": "Client Audit/policy.pdf", "size": 12, "last_modified": 10},
    ]
    created = intake.resolve_source(ws, "Client Audit", manifest)
    assert created["label"] == "Client Audit"
    assert created["root_name"] == "Client Audit"

    index = intake.load_index(ws)
    index["files"].append(
        {
            "source_id": created["id"],
            "relative_path": "Client Audit/ledger.csv",
            "size": 8,
            "last_modified": 10,
            "sha1": "old",
        }
    )
    intake.save_index(ws, index)

    resolved = intake.resolve_source(ws, "client audit", manifest)
    assert resolved["id"] == created["id"]
    assert len(intake.list_sources(ws)) == 1


def test_folder_source_reuses_one_empty_interrupted_source():
    ws = _workspace()
    interrupted = intake.create_source(ws, "Client Audit", "Client Audit")
    resolved = intake.resolve_source(
        ws,
        "Client Audit",
        [{"relative_path": "Client Audit/ledger.csv", "size": 8, "last_modified": 10}],
    )
    assert resolved["id"] == interrupted["id"]


def test_ambiguous_same_named_folders_create_a_new_source():
    ws = _workspace()
    first = intake.create_source(ws, "Client Audit", "Client Audit")
    second = intake.create_source(ws, "Client Audit", "Client Audit")
    index = intake.load_index(ws)
    index["files"].extend(
        [
            {"source_id": first["id"], "relative_path": "Client Audit/a.csv"},
            {"source_id": second["id"], "relative_path": "Client Audit/b.csv"},
        ]
    )
    intake.save_index(ws, index)

    resolved = intake.resolve_source(
        ws,
        "Client Audit",
        [{"relative_path": "Client Audit/new.csv", "size": 8, "last_modified": 10}],
    )
    assert resolved["id"] not in {first["id"], second["id"]}
    assert len(intake.list_sources(ws)) == 3


def test_manifest_rejects_traversal_and_absolute_paths():
    ws = _workspace()
    source = intake.create_source(ws, "Audit folder")
    for path in ("../secret.csv", "/tmp/secret.csv", "C:/secret.csv"):
        try:
            intake.compare_manifest(
                ws,
                source["id"],
                [{"relative_path": path, "size": 1, "last_modified": 1}],
            )
        except workspaces.WorkspaceError:
            pass
        else:
            raise AssertionError(f"unsafe path was accepted: {path}")


def test_safe_classification_payload_has_schema_but_no_rows_or_staging_path():
    ws = _workspace()
    content = b"account,amount\nVendor A,100\nVendor B,250\n"
    _, batch = _source_and_batch(
        ws,
        [{"relative_path": "Audit/ledger.csv", "size": len(content), "last_modified": 10}],
    )
    _stage(ws, batch, "Audit/ledger.csv", content)
    batch = intake.complete_upload(ws, batch["id"])
    payload = intake.classification_payload_for_model(ws, batch)
    serialized = str(payload)
    assert "account" in serialized and "amount" in serialized
    assert "Vendor A" not in serialized and "250" not in serialized
    assert "Staging" not in serialized
    assert str(ws.root) not in serialized


def test_same_hash_candidates_are_proposed_as_duplicates_once():
    ws = _workspace()
    content = b"id,amount\n1,10\n"
    _, batch = _source_and_batch(
        ws,
        [
            {"relative_path": "Audit/ledger.csv", "size": len(content), "last_modified": 1},
            {"relative_path": "Audit/copy.csv", "size": len(content), "last_modified": 1},
        ],
        "auto",
    )
    _stage(ws, batch, "Audit/ledger.csv", content)
    batch = intake.load_batch(ws, batch["id"])
    _stage(ws, batch, "Audit/copy.csv", content)
    completed = intake.complete_upload(ws, batch["id"])
    duplicates = [item for item in completed["items"] if item["classification"]["duplicate_ref"]]
    assert len(duplicates) == 1
    assert duplicates[0]["classification"]["proposed_action"] == "ignore"


def test_apply_batch_imports_table_and_replaces_changed_document_in_place():
    ws = _workspace()
    csv = b"id,amount\n1,10\n"
    source, batch = _source_and_batch(
        ws,
        [{"relative_path": "Audit/ledger.csv", "size": len(csv), "last_modified": 1}],
        "auto",
    )
    _stage(ws, batch, "Audit/ledger.csv", csv)
    intake.complete_upload(ws, batch["id"])
    result = intake.apply_batch(ws, batch["id"])
    assert result["summary"]["imported"] == 1
    reloaded = workspaces.load_workspace(ws.id)
    assert reloaded.tables[0]["source_id"] == source["id"]
    assert reloaded.tables[0]["source_sha1"] == hashlib.sha1(csv).hexdigest()

    doc1 = b"Initial policy text"
    batch = intake.compare_manifest(
        reloaded,
        source["id"],
        [{"relative_path": "Audit/policy.txt", "size": len(doc1), "last_modified": 2}],
        "auto",
    )["batch"]
    _stage(reloaded, batch, "Audit/policy.txt", doc1)
    intake.complete_upload(reloaded, batch["id"])
    policy_result = intake.apply_batch(reloaded, batch["id"])
    original_document_id = policy_result["items"][0]["target_ref"].split(":", 1)[1]
    recommendation = policy_result["suggested_actions"][0]
    assert recommendation["agent_kind"] == "planning"
    assert recommendation["document_ids"] == [policy_result["items"][0]["target_ref"].split(":", 1)[1]]
    assert recommendation["requires_doc_ai"] is True

    doc2 = b"Changed policy text"
    batch = intake.compare_manifest(
        reloaded,
        source["id"],
        [{"relative_path": "Audit/policy.txt", "size": len(doc2), "last_modified": 3}],
        "auto",
    )["batch"]
    _stage(reloaded, batch, "Audit/policy.txt", doc2)
    intake.complete_upload(reloaded, batch["id"])
    intake.apply_batch(reloaded, batch["id"])
    final = workspaces.load_workspace(ws.id)
    assert len(final.documents) == 1
    assert final.documents[0]["id"] == original_document_id
    assert final.documents[0]["sha1"] == hashlib.sha1(doc2).hexdigest()
    assert "version" not in final.documents[0]


def test_changed_folder_table_replaces_in_place_and_keeps_history():
    ws = _workspace()
    first = b"id,amount\n1,10\n"
    source, batch = _source_and_batch(
        ws,
        [{"relative_path": "Audit/ledger.csv", "size": len(first), "last_modified": 1}],
        "auto",
    )
    _stage(ws, batch, "Audit/ledger.csv", first)
    intake.complete_upload(ws, batch["id"])
    intake.apply_batch(ws, batch["id"])
    original_name = ws.tables[0]["name"]

    changed = b"id,amount\n1,10\n2,20\n"
    batch = intake.compare_manifest(
        ws,
        source["id"],
        [{"relative_path": "Audit/ledger.csv", "size": len(changed), "last_modified": 2}],
        "auto",
    )["batch"]
    _stage(ws, batch, "Audit/ledger.csv", changed)
    intake.complete_upload(ws, batch["id"])
    intake.apply_batch(ws, batch["id"])
    reloaded = workspaces.load_workspace(ws.id)
    assert [table["name"] for table in reloaded.tables] == [original_name]
    assert reloaded.get_frame(original_name).height == 2
    record = next(
        item
        for item in intake.load_index(reloaded)["files"]
        if item["relative_path"] == "Audit/ledger.csv"
    )
    assert record["history"][0]["sha1"] == hashlib.sha1(first).hexdigest()
    assert record["sha1"] == hashlib.sha1(changed).hexdigest()


def test_folder_upload_api_streams_requested_file():
    ws = _workspace()
    source = intake.create_source(ws, "Audit folder")
    content = b"id,amount\n1,10\n"
    compared = intake.compare_manifest(
        ws,
        source["id"],
        [{"relative_path": "Audit/ledger.csv", "size": len(content), "last_modified": 1}],
    )
    client = TestClient(create_app())
    response = client.post(
        f"/api/workspaces/{ws.id}/folder-imports/{compared['batch']['id']}/files",
        data={"relative_path": "Audit/ledger.csv"},
        files={"file": ("ledger.csv", content, "text/csv")},
    )
    assert response.status_code == 200
    completed = client.post(
        f"/api/workspaces/{ws.id}/folder-imports/{compared['batch']['id']}/complete-upload"
    )
    assert completed.status_code == 200
    assert completed.json()["items"][0]["local_metadata"]["route"] == "table"


def test_folder_import_api_hides_source_creation():
    ws = _workspace()
    client = TestClient(create_app())
    response = client.post(
        f"/api/workspaces/{ws.id}/folder-imports",
        json={
            "root_name": "Client Audit",
            "mode": "auto",
            "manifest": [
                {
                    "relative_path": "Client Audit/ledger.csv",
                    "size": 8,
                    "last_modified": 10,
                    "mime": "text/csv",
                }
            ],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["upload_paths"] == ["Client Audit/ledger.csv"]
    assert payload["batch"]["source_id"] == intake.list_sources(ws)[0]["id"]


def test_intake_run_needs_no_table_or_configured_model(monkeypatch):
    ws = _workspace()
    content = b"id,amount\n1,10\n"
    source, batch = _source_and_batch(
        ws,
        [{"relative_path": "Audit/ledger.csv", "size": len(content), "last_modified": 1}],
        "auto",
    )
    _stage(ws, batch, "Audit/ledger.csv", content)
    intake.complete_upload(ws, batch["id"])
    monkeypatch.setattr(
        "app.llm.agent_status",
        lambda: {"configured": False, "backend": "", "model": ""},
    )
    run = runner.start_run(
        ws,
        "auto",
        {"batch_id": batch["id"], "source_id": source["id"]},
        kind="intake",
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = store.load_run(ws, run["id"])
        if current["status"] in store.TERMINAL_STATUSES:
            break
        time.sleep(0.02)
    assert current["status"] == "completed", current.get("error")
    assert current["kind"] == "intake"
    assert current["intake"]["imported"] == 1
    assert workspaces.load_workspace(ws.id).tables


def test_intake_run_uses_model_file_classification(fake_agent_llm):
    ws = _workspace()
    content = b"Procurement policy requires approval before commitment.\n"
    source, batch = _source_and_batch(
        ws,
        [{"relative_path": "Audit/guidance.txt", "size": len(content), "last_modified": 1}],
        "auto",
    )
    _stage(ws, batch, "Audit/guidance.txt", content)
    batch = intake.complete_upload(ws, batch["id"])
    item_id = batch["items"][0]["id"]
    fake_agent_llm.overrides["agent:file_classification"] = {
        "items": [
            {
                "id": item_id,
                "route": "document",
                "document_category": "policy",
                "confidence": "high",
                "rationale": "The filename indicates audit guidance.",
                "proposed_action": "import",
            }
        ]
    }

    run = runner.start_run(
        ws,
        "auto",
        {"batch_id": batch["id"], "source_id": source["id"]},
        kind="intake",
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = store.load_run(ws, run["id"])
        if current["status"] in store.TERMINAL_STATUSES:
            break
        time.sleep(0.02)

    imported = intake.load_batch(ws, batch["id"])
    document = workspaces.load_workspace(ws.id).documents[0]
    assert current["status"] == "completed", current.get("error")
    assert [call["tag"] for call in fake_agent_llm.calls] == ["agent:file_classification"]
    assert imported["items"][0]["classification"]["document_category"] == "policy"
    assert document["category"] == "policy"


def test_rootless_batches_share_one_direct_uploads_source():
    ws = _workspace()
    manifest = [{"relative_path": "ledger.csv", "size": 8, "last_modified": 10}]
    first = intake.resolve_source(ws, "", manifest)
    assert first["label"] == intake.DIRECT_UPLOADS_LABEL
    assert first["root_name"] == ""
    second = intake.resolve_source(ws, "", [{"relative_path": "policy.pdf", "size": 4, "last_modified": 11}])
    assert second["id"] == first["id"]
    assert len(intake.list_sources(ws)) == 1


def test_deterministic_classification_uses_document_confidence_tiers_only():
    def classify(path, route="table"):
        return intake.deterministic_classification({
            "relative_path": path,
            "local_metadata": {"route": route, "parse_ok": True},
        })

    known_table = classify("Data/vendor master list.xlsx")
    generic_table = classify("Data/q3_extract.csv")
    assert known_table["route"] == "table"
    assert generic_table["route"] == "table"
    assert "table_role" not in known_table
    assert "table_role" not in generic_table
    assert known_table["confidence"] == "high"
    assert generic_table["confidence"] == "high"

    known_category = classify("Docs/procurement policy.pdf", route="document")
    unknown_category = classify("Docs/scan0001.pdf", route="document")
    assert known_category["confidence"] == "high"
    assert unknown_category["document_category"] == "other"
    assert unknown_category["confidence"] == "medium"


def test_apply_endpoint_imports_files_without_an_agent_run():
    ws = _workspace()
    csv = b"id,amount\n1,10\n"
    text = b"Procurement policy requires approval before commitment.\n"
    client = TestClient(create_app())
    compared = client.post(
        f"/api/workspaces/{ws.id}/folder-imports",
        json={
            "root_name": "",
            "mode": "permission",
            "manifest": [
                {"relative_path": "ledger.csv", "size": len(csv), "last_modified": 1},
                {"relative_path": "scan0001.txt", "size": len(text), "last_modified": 1},
            ],
        },
    ).json()
    batch_id = compared["batch"]["id"]
    for name, content, mime in (("ledger.csv", csv, "text/csv"), ("scan0001.txt", text, "text/plain")):
        response = client.post(
            f"/api/workspaces/{ws.id}/folder-imports/{batch_id}/files",
            data={"relative_path": name},
            files={"file": (name, content, mime)},
        )
        assert response.status_code == 200
    completed = client.post(
        f"/api/workspaces/{ws.id}/folder-imports/{batch_id}/complete-upload"
    ).json()
    document_item = next(item for item in completed["items"] if item["relative_path"] == "scan0001.txt")
    assert document_item["classification"]["document_category"] == "other"

    applied = client.post(
        f"/api/workspaces/{ws.id}/folder-imports/{batch_id}/apply",
        json={"decisions": [{"item_id": document_item["id"], "document_category": "evidence"}]},
    )
    assert applied.status_code == 200
    assert applied.json()["summary"]["imported"] == 2
    reloaded = workspaces.load_workspace(ws.id)
    assert [table["source_id"] for table in reloaded.tables] == [compared["batch"]["source_id"]]
    assert reloaded.documents[0]["category"] == "evidence"
