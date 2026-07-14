"""Finding and audit-report endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Body

from .. import doc_tests, findings, report, workspaces

router = APIRouter(prefix="/api/workspaces/{workspace_id}", tags=["findings", "report"])


def _ws(workspace_id: str):
    return workspaces.load_workspace(workspace_id)


@router.get("/findings")
async def list_findings(workspace_id: str):
    ws = _ws(workspace_id)
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
    return {
        "items": ws.findings,
        "rcm": ws.rcm,
        "procedures": ws.work_program,
        "rollups": findings.rollups(ws),
        "evidence_options": evidence_options,
    }


@router.post("/findings")
async def add_finding(workspace_id: str, payload: dict = Body(...)):
    return findings.add(_ws(workspace_id), payload, source="manual")


@router.post("/findings/promote")
async def promote_finding(workspace_id: str, payload: dict = Body(...)):
    return findings.promote(
        _ws(workspace_id), str(payload.get("run_id") or ""), str(payload.get("finding_id") or "")
    )


@router.patch("/findings/{finding_id}")
async def patch_finding(workspace_id: str, finding_id: str, payload: dict = Body(...)):
    return findings.update(_ws(workspace_id), finding_id, payload)


@router.delete("/findings/{finding_id}")
async def delete_finding(workspace_id: str, finding_id: str):
    ws = _ws(workspace_id)
    findings.remove(ws, finding_id)
    return {"ok": True}


@router.get("/report")
async def get_report(workspace_id: str):
    return report.payload(_ws(workspace_id))


@router.get("/report/context")
async def get_report_context(workspace_id: str):
    return report.build_context(_ws(workspace_id))


@router.patch("/report")
async def patch_report(workspace_id: str, payload: dict = Body(...)):
    return report.update(_ws(workspace_id), payload)


@router.post("/report/generate")
async def generate_report(workspace_id: str, payload: dict = Body(default={})):
    return report.generate(
        _ws(workspace_id),
        use_model=payload.get("use_model") is not False,
        run_id=str(payload.get("run_id") or "") or None,
    )


@router.post("/report/reconcile")
async def reconcile_report(workspace_id: str, payload: dict = Body(...)):
    return report.reconcile(_ws(workspace_id), str(payload.get("action") or ""))


@router.post("/report/quality")
async def check_report_quality(workspace_id: str, payload: dict = Body(default={})):
    ws = _ws(workspace_id)
    return report.editorial_review(ws) if payload.get("editorial") else report.quality_checks(ws)
