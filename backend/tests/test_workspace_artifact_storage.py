"""Regression coverage for the schema-v4 audit-artifact sidecar layout."""

from __future__ import annotations

import json

import pytest

from app import workspaces


def test_legacy_audit_records_migrate_to_independent_artifact_files():
    ws = workspaces.create_workspace("Sidecar migration")
    legacy = {
        "schema_version": 3,
        "revision": 7,
        "id": ws.id,
        "name": ws.name,
        "description": "",
        "created": ws.created,
        "tables": [], "joins": [], "tiles": [], "analyses": [], "rulesets": [], "documents": [],
        "planning": {"context": {"entity": "Example Co"}, "apm_markdown": "# APM"},
        "rcm": [{"id": "RCM-1", "process": "Purchasing", "risk": "Unsupported spend"}],
        "data_tests": [{"id": "DAT-1", "title": "Approval test"}],
        "work_program": [{"id": "PROC-1", "objective": "Legacy procedure"}],
        "observations": [{"id": "OBS-1", "summary": "Exception"}],
        "evidence_requests": [{"id": "ER-1", "status": "open"}],
        "findings": [{"id": "F-1", "title": "Missing approval"}],
        "report": {"markdown": "# Draft report"},
        "dashboard_advice": {"note": "Review coverage"},
    }
    ws.definition_path.write_text(json.dumps(legacy), encoding="utf-8")

    migrated = workspaces.Workspace(ws.root)

    assert migrated.schema_version == 4
    assert migrated.planning["apm_markdown"] == "# APM"
    assert [item["id"] for item in migrated.rcm] == ["RCM-1"]
    assert [item["id"] for item in migrated.data_tests] == ["DAT-1"]
    assert (ws.root / "Planning" / "APM.md").read_text(encoding="utf-8") == "# APM"
    assert (ws.root / "Planning" / "RCM" / "RCM-1.json").is_file()
    assert json.loads((ws.root / "Planning" / "RCM" / ".index.json").read_text()) == {"ids": ["RCM-1"]}
    assert (ws.root / "DataTests" / "DAT-1.json").is_file()
    assert (ws.root / "Findings" / "F-1.json").is_file()
    assert (ws.root / "Reports" / "current.json").is_file()

    manifest = json.loads(ws.definition_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 4
    assert not ({"planning", "rcm", "data_tests", "findings", "report"} & set(manifest))


def test_sidecar_mutations_reload_without_embedded_workspace_records():
    ws = workspaces.create_workspace("Sidecar reload")
    ws.update_planning({"context": {"entity": "Example Co"}, "apm_markdown": "# Plan"})
    row = ws.add_rcm({"process": "Revenue", "risk": "Revenue is incomplete"})
    ws.findings.append({"id": "F-1", "title": "Draft", "evidence_refs": []})
    ws.save()

    reloaded = workspaces.Workspace(ws.root)

    assert reloaded.planning["context"]["entity"] == "Example Co"
    assert reloaded.rcm[0]["id"] == row["id"]
    assert json.loads((ws.root / "Planning" / "RCM" / ".index.json").read_text()) == {"ids": [row["id"]]}
    assert reloaded.findings[0]["title"] == "Draft"
    assert "rcm" not in json.loads(ws.definition_path.read_text(encoding="utf-8"))


def test_failed_manifest_write_rolls_back_all_new_sidecars(monkeypatch):
    ws = workspaces.create_workspace("Sidecar rollback")
    original_write = workspaces.write_json_atomic

    def fail_manifest(path, payload):
        if path == ws.definition_path:
            raise OSError("simulated manifest failure")
        original_write(path, payload)

    monkeypatch.setattr(workspaces, "write_json_atomic", fail_manifest)
    with pytest.raises(OSError, match="manifest failure"):
        ws.add_rcm({"id": "RCM-ROLLBACK", "process": "Cash", "risk": "Misstatement"})

    assert not (ws.root / "Planning" / "RCM" / "RCM-ROLLBACK.json").exists()
    assert not list((ws.root / ".Transactions").glob("txn-*.json"))
