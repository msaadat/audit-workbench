"""Finding and audit-report endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Body

from .. import doc_tests, findings, report, workspaces
from ..evidence import normalize_anchor

router = APIRouter(prefix="/api/workspaces/{workspace_id}", tags=["findings", "report"])


def _ws(workspace_id: str):
    return workspaces.load_workspace(workspace_id)


@router.get("/findings")
def list_findings(workspace_id: str):
    ws = _ws(workspace_id)
    # ``list_tests`` runs twice here and ``load_test`` once per test, and the
    # cycle-vouching materialization underneath each re-reads the whole
    # evidence corpus. This handler only reads, so one scope covers them all.
    with doc_tests.request_cache_scope():
        evidence_options: list[dict] = []
        seen: set[str] = set()
        for procedure in ws.work_program:
            for anchor in procedure.get("evidence_refs") or []:
                key = str(anchor.get("id"))
                if key not in seen:
                    evidence_options.append({"anchor": anchor, "label": f"{procedure['id']} · {anchor.get('source_kind')}:{anchor.get('source_id')}"})
                    seen.add(key)
        for summary in doc_tests.list_tests(ws):
            test = doc_tests.load_test(ws, summary["id"])
            for item in test.get("items") or []:
                for anchor in item.get("evidence_refs") or []:
                    key = str(anchor.get("id"))
                    if key not in seen:
                        evidence_options.append({"anchor": anchor, "label": f"{test['id']} · {item.get('label') or item['id']} · {anchor.get('source_kind')}:{anchor.get('source_id')}"})
                        seen.add(key)
        for data_test in ws.data_tests:
            last_run = data_test.get("last_run")
            if not last_run:
                continue
            anchor = normalize_anchor(
                {
                    "source_kind": "datatest",
                    "source_id": f"{data_test['id']}:{last_run['id']}",
                    "source_sha1": last_run["result_sha1"],
                },
                require_hash=True,
            )
            evidence_options.append(
                {"anchor": anchor, "label": f"{data_test['id']} · durable result {last_run['id']}"}
            )
        return {
            "items": [
                {**item, "evidence_warnings": findings.evidence_warnings(ws, item)}
                for item in ws.findings
            ],
            "rcm": ws.rcm,
            "procedures": ws.work_program,
            "data_tests": ws.data_tests,
            "document_tests": doc_tests.list_tests(ws),
            "rollups": findings.rollups(ws),
            "evidence_options": evidence_options,
        }


@router.post("/findings")
def add_finding(workspace_id: str, payload: dict = Body(...)):
    return findings.add(_ws(workspace_id), payload, source="manual")


@router.post("/findings/promote")
def promote_finding(workspace_id: str, payload: dict = Body(...)):
    return findings.promote(
        _ws(workspace_id), str(payload.get("run_id") or ""), str(payload.get("finding_id") or "")
    )


@router.patch("/findings/{finding_id}")
def patch_finding(workspace_id: str, finding_id: str, payload: dict = Body(...)):
    ws = _ws(workspace_id)
    item = findings.update(ws, finding_id, payload)
    return {**item, "evidence_warnings": findings.evidence_warnings(ws, item)}


@router.post("/findings/{finding_id}/evidence/reaffirm")
def reaffirm_finding_evidence(workspace_id: str, finding_id: str, payload: dict = Body(default={})):
    ws = _ws(workspace_id)
    item = findings.reaffirm_evidence(
        ws, finding_id, str(payload.get("evidence_id") or "") or None
    )
    return {**item, "evidence_warnings": findings.evidence_warnings(ws, item)}


@router.delete("/findings/{finding_id}")
def delete_finding(workspace_id: str, finding_id: str):
    ws = _ws(workspace_id)
    findings.remove(ws, finding_id)
    return {"ok": True}


@router.get("/report")
def get_report(workspace_id: str):
    return report.payload(_ws(workspace_id))


@router.get("/report/context")
def get_report_context(workspace_id: str):
    return report.build_context(_ws(workspace_id))


@router.patch("/report")
def patch_report(workspace_id: str, payload: dict = Body(...)):
    return report.update(_ws(workspace_id), payload)


@router.post("/report/generate")
def generate_report(workspace_id: str, payload: dict = Body(default={})):
    return report.generate(
        _ws(workspace_id),
        use_model=payload.get("use_model") is not False,
        run_id=str(payload.get("run_id") or "") or None,
    )


@router.post("/report/reconcile")
def reconcile_report(workspace_id: str, payload: dict = Body(...)):
    return report.reconcile(_ws(workspace_id), str(payload.get("action") or ""))


@router.post("/report/quality")
def check_report_quality(workspace_id: str, payload: dict = Body(default={})):
    ws = _ws(workspace_id)
    return report.editorial_review(ws) if payload.get("editorial") else report.quality_checks(ws)
