import json

from app import doc_tests, workspaces


def _write_legacy_workspace(tmp_path, *, rcm, procedures, findings=None):
    root = tmp_path / "Workspaces" / "legacy-engagement"
    (root / "Data").mkdir(parents=True)
    (root / "workspace.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "id": "legacy-engagement",
                "name": "Legacy engagement",
                "description": "Migration fixture",
                "created": "2026-01-01",
                "tables": [],
                "joins": [],
                "rcm": rcm,
                "work_program": procedures,
                "findings": findings or [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return root


def _rcm(row_id, risk):
    return {
        "id": row_id,
        "semantic_id": f"rcm:{row_id.lower()}",
        "process": "Procurement",
        "risk": risk,
        "risk_rating": "high",
        "assertion": "Authorization",
        "control": "Management approval",
        "control_type": "Preventive",
        "test_procedure": "Inspect approval evidence.",
        "test_refs": [],
    }


def _procedure(procedure_id, refs, objective=None):
    return {
        "id": procedure_id,
        "semantic_id": f"procedure:{procedure_id.lower()}",
        "rcm_refs": refs,
        "objective": objective or f"Test {procedure_id}",
        "criteria": "Procurement policy requires approval.",
        "steps": ["Select transactions.", "Inspect approval evidence."],
        "method": "Data analytics and document inspection",
        "expected_evidence": "Exception output and approval record.",
        "test_refs": [],
        "evidence_refs": [],
        "result_summary": "",
        "conclusion": "",
        "scope_limitations": "",
    }


def test_schema_v1_migrates_one_to_one_and_many_procedures_without_data_loss(tmp_path):
    rows = [_rcm("RCM-1", "Unauthorized purchases")]
    procedures = [_procedure("PROC-1", ["RCM-1"]), _procedure("PROC-2", ["RCM-1"])]
    root = _write_legacy_workspace(tmp_path, rcm=rows, procedures=procedures)

    ws = workspaces.Workspace(root)

    assert len(ws.work_program) == 2
    assert len(ws.rcm[0]["planned_tests"]) == 2
    assert {item["legacy_procedure_id"] for item in ws.rcm[0]["planned_tests"]} == {
        "PROC-1",
        "PROC-2",
    }
    assert all(item["method"] == "hybrid" for item in ws.rcm[0]["planned_tests"])
    assert ws.rcm_migration["review_queue"] == []
    assert ws.rcm_migration["unassigned_procedures"] == []

    ws.save()
    saved = json.loads((root / "workspace.json").read_text(encoding="utf-8"))
    assert saved["schema_version"] == 2
    assert [item["id"] for item in saved["work_program"]] == ["PROC-1", "PROC-2"]
    assert [item["objective"] for item in saved["work_program"]] == [
        procedure["objective"] for procedure in procedures
    ]
    reloaded = workspaces.Workspace(root)
    assert len(reloaded.rcm[0]["planned_tests"]) == 2


def test_schema_v1_queues_multi_rcm_and_unlinked_procedures_for_review(tmp_path):
    rows = [_rcm("RCM-1", "Unauthorized purchases"), _rcm("RCM-2", "Duplicate payments")]
    procedures = [
        _procedure("PROC-MULTI", ["RCM-1", "RCM-2"]),
        _procedure("PROC-UNLINKED", []),
        _procedure("PROC-MISSING", ["RCM-404"]),
    ]
    root = _write_legacy_workspace(tmp_path, rcm=rows, procedures=procedures)

    ws = workspaces.Workspace(root)

    assert all(row["planned_tests"] == [] for row in ws.rcm)
    assert {item["procedure_id"] for item in ws.rcm_migration["review_queue"]} == {
        "PROC-MULTI",
        "PROC-MISSING",
    }
    assert ws.rcm_migration["unassigned_procedures"] == [
        {"procedure_id": "PROC-UNLINKED", "reason": "No RCM reference."}
    ]


def test_legacy_document_test_and_finding_resolve_to_planned_test(tmp_path):
    rows = [_rcm("RCM-1", "Unauthorized purchases")]
    procedures = [_procedure("PROC-1", ["RCM-1"])]
    findings = [
        {
            "id": "F-1",
            "title": "Missing approval",
            "severity": "high",
            "rcm_refs": ["RCM-1"],
            "procedure_refs": ["PROC-1"],
            "evidence_refs": [],
        }
    ]
    root = _write_legacy_workspace(tmp_path, rcm=rows, procedures=procedures, findings=findings)
    (root / "DocTests").mkdir()
    (root / "DocTests" / "DT-1.json").write_text(
        json.dumps(
            {
                "id": "DT-1",
                "kind": "review",
                "title": "Approval inspection",
                "rcm_refs": ["RCM-1"],
                "procedure_refs": ["PROC-1"],
                "items": [],
            }
        ),
        encoding="utf-8",
    )

    ws = workspaces.Workspace(root)
    target = ws.rcm_migration["migrated_procedures"]["PROC-1"]
    test = doc_tests.load_test(ws, "DT-1")

    assert test["rcm_id"] == "RCM-1"
    assert test["planned_test_id"] == target["planned_test_id"]
    assert ws.findings[0]["planned_test_refs"] == [target["planned_test_id"]]


def test_procurement_sized_fixture_migrates_eleven_procedures(tmp_path):
    rows = [_rcm(f"RCM-{index}", f"Procurement risk {index}") for index in range(1, 12)]
    procedures = [
        _procedure(f"PROC-{index}", [f"RCM-{index}"])
        for index in range(1, 12)
    ]
    root = _write_legacy_workspace(tmp_path, rcm=rows, procedures=procedures)

    ws = workspaces.Workspace(root)

    assert len(ws.rcm) == 11
    assert sum(len(row["planned_tests"]) for row in ws.rcm) == 11
    assert len(ws.rcm_migration["migrated_procedures"]) == 11


def test_linked_legacy_analysis_and_ruleset_migrate_while_ambiguous_work_is_queued(tmp_path):
    root = _write_legacy_workspace(
        tmp_path,
        rcm=[_rcm("RCM-1", "Unauthorized purchases")],
        procedures=[_procedure("PROC-1", ["RCM-1"])],
    )
    (root / "Data" / "transactions.csv").write_text(
        "invoice_no,amount\nINV-1,100\nINV-2,200\n", encoding="utf-8"
    )
    path = root / "workspace.json"
    definition = json.loads(path.read_text(encoding="utf-8"))
    definition.update(
        tables=[{"name": "transactions", "file": "transactions.csv", "source": "transactions.csv"}],
        analyses=[
            {
                "id": "AN-1", "kind": "analytics", "title": "Invoice completeness",
                "table": "transactions", "procedure_refs": ["PROC-1"],
                "spec": {"test": "completeness", "params": {"columns": ["invoice_no"]}},
            },
            {
                "id": "AN-UNASSIGNED", "kind": "analytics", "title": "Unassigned analysis",
                "table": "transactions",
                "spec": {"test": "sign_scan", "params": {"column": "amount"}},
            },
        ],
        rulesets=[{
            "id": "RULE-1", "title": "Required invoice number", "table": "transactions",
            "procedure_refs": ["PROC-1"],
            "rules": [{"column": "invoice_no", "check": "required"}],
        }],
    )
    path.write_text(json.dumps(definition, indent=2), encoding="utf-8")

    ws = workspaces.Workspace(root)

    assert len(ws.data_tests) == 2
    assert {item["engine"] for item in ws.data_tests} == {"analytics", "validation"}
    assert all(item["status"] == "ready" and item["last_run"] is None for item in ws.data_tests)
    assert {item["legacy_source_id"] for item in ws.data_tests} == {"AN-1", "RULE-1"}
    assert ws.rcm_migration["unassigned_tests"] == [{
        "kind": "analysis", "id": "AN-UNASSIGNED",
        "reason": "No unambiguous RCM planned-test parent.",
    }]
    planned = ws.rcm[0]["planned_tests"][0]
    assert {ref.split(":", 1)[0] for ref in planned["execution_refs"]} == {"datatest"}
    assert len(planned["execution_refs"]) == 2
    assert [item["id"] for item in ws.analyses] == ["AN-1", "AN-UNASSIGNED"]
    assert [item["id"] for item in ws.rulesets] == ["RULE-1"]


def test_planned_test_crud_validates_parent_and_preserves_execution_links(tmp_path):
    root = _write_legacy_workspace(
        tmp_path,
        rcm=[_rcm("RCM-1", "Unauthorized purchases")],
        procedures=[],
    )
    ws = workspaces.Workspace(root)
    planned = ws.add_planned_test(
        "RCM-1",
        {
            "title": "Approval completeness",
            "objective": "Identify transactions without approval.",
            "method": "data_analytics",
            "steps": ["Scan the population."],
        },
    )
    assert planned["status"] == "not_ready"
    assert planned["control_conclusion"] == "no_conclusion"

    updated = ws.update_planned_test(
        "RCM-1", planned["id"], {"status": "ready", "execution_refs": ["datatest:DAT-1"]}
    )
    assert updated["status"] == "ready"
    assert updated["execution_refs"] == ["datatest:DAT-1"]
