"""Finding and audit-report endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Body

from .. import doc_tests, finding_consolidation, findings, report, workspaces
from ..agent import runner
from ..workspaces import WorkspaceError

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
            # Stamp the option with the hash the staleness checks resolve, not
            # the run's ``result_sha1`` file-integrity hash: an anchor pins the
            # narrower evidentiary projection, so the two never agree and every
            # freshly picked data-test anchor read as already stale.
            anchor = findings.anchor_from_ref(
                ws, f"datatest:{data_test['id']}:{last_run['id']}"
            )
            if anchor is None:
                continue
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


# --------------------------------------------------------------------------- #
# Consolidation: suggestions the model made, decisions the auditor makes
# --------------------------------------------------------------------------- #
def _finding_payload(ws, item: dict) -> dict:
    return {**item, "evidence_warnings": findings.evidence_warnings(ws, item)}


@router.get("/findings/consolidation")
def get_consolidation(workspace_id: str):
    """The current basis, its suggestion set (or null), and each group's members."""
    ws = _ws(workspace_id)
    with doc_tests.request_cache_scope():
        return finding_consolidation.summary(ws)


@router.post("/findings/consolidation/refresh")
def refresh_consolidation(workspace_id: str, payload: dict = Body(default={})):
    """Queue a ``findings.consolidated`` run for the current draft set.

    The same path "Generate all findings" takes: a workflow command naming
    the outcome, started on the run thread. ``force`` re-asks even when a
    suggestion for the current basis is already on file.
    """
    ws = _ws(workspace_id)
    mode = str(payload.get("mode") or "auto")
    try:
        return runner.start_command_run(
            ws,
            mode if mode in {"auto", "permission"} else "auto",
            {
                "source": "tab_button",
                "text": "Review the draft findings for consolidation.",
                "goal_template": "finding_consolidation",
                "requested_outcomes": ["findings.consolidated"],
                "target_refs": [],
                "generation_mode": "force" if payload.get("force") else "reuse_existing",
            },
            context=dict(payload.get("context") or {}),
        )
    except runner.AgentBusyError as error:
        raise WorkspaceError(str(error)) from error


def _group(ws, group_id: str) -> tuple[str, dict]:
    basis = finding_consolidation.basis_sha1(ws)
    suggestion = finding_consolidation.load(ws, basis)
    if suggestion is None:
        raise WorkspaceError(
            "The findings have changed since the last consolidation review; refresh the suggestions."
        )
    group = next(
        (item for item in suggestion.get("groups") or [] if item.get("group_id") == group_id),
        None,
    )
    if group is None:
        raise WorkspaceError(f"Consolidation group '{group_id}' is not in the current suggestions.")
    return basis, group


@router.post("/findings/consolidation/{group_id}/accept")
def accept_consolidation(workspace_id: str, group_id: str, payload: dict = Body(default={})):
    """Merge a suggested group into its lead; the suggestion records the decision."""
    ws = _ws(workspace_id)
    basis, group = _group(ws, group_id)
    if group.get("decision"):
        raise WorkspaceError(f"Consolidation group '{group_id}' was already {group['decision']}.")
    lead = findings.consolidate(
        ws,
        finding_ids=list(group.get("finding_ids") or []),
        lead_id=str(payload.get("lead_finding_id") or group.get("lead_finding_id") or ""),
        relation=str(group.get("relation") or ""),
        group_id=group_id,
        basis=basis,
        title=str(payload.get("title") or group.get("proposed_title") or "") or None,
        root_cause_hypothesis=str(group.get("root_cause_hypothesis") or "") or None,
        decided_by="auditor",
        include_confirmed=bool(payload.get("include_confirmed")),
    )
    finding_consolidation.decide(ws, basis, group_id, "accepted", decided_by="auditor")
    return _finding_payload(ws, lead)


@router.post("/findings/consolidation/{group_id}/dismiss")
def dismiss_consolidation(workspace_id: str, group_id: str):
    ws = _ws(workspace_id)
    basis, _group_record = _group(ws, group_id)
    finding_consolidation.decide(ws, basis, group_id, "dismissed", decided_by="auditor")
    return finding_consolidation.summary(ws)


@router.post("/findings/consolidate")
def consolidate_findings(workspace_id: str, payload: dict = Body(...)):
    """An auditor-made group, through the same merge as an accepted suggestion."""
    ws = _ws(workspace_id)
    finding_ids = [str(value) for value in payload.get("finding_ids") or []]
    lead_id = str(payload.get("lead_finding_id") or (finding_ids[0] if finding_ids else ""))
    relation = str(payload.get("relation") or "shared_cause")
    basis = finding_consolidation.basis_sha1(ws)
    group_id = finding_consolidation.group_id(finding_ids)
    lead = findings.consolidate(
        ws,
        finding_ids=finding_ids,
        lead_id=lead_id,
        relation=relation,
        group_id=group_id,
        basis=basis,
        title=str(payload.get("title") or "") or None,
        root_cause_hypothesis=str(payload.get("root_cause_hypothesis") or "") or None,
        decided_by="auditor",
        include_confirmed=bool(payload.get("include_confirmed")),
    )
    finding_consolidation.record_manual_group(
        ws,
        basis,
        {
            "group_id": group_id,
            "finding_ids": finding_ids,
            "lead_finding_id": lead_id,
            "relation": relation,
            "basis": "entity" if payload.get("basis") == "entity" else "process",
            "proposed_title": str(payload.get("title") or lead.get("title") or ""),
            "root_cause_hypothesis": str(payload.get("root_cause_hypothesis") or ""),
            "rationale": str(payload.get("rationale") or "Grouped by the auditor."),
        },
    )
    return _finding_payload(ws, lead)


@router.post("/findings/{finding_id}/unconsolidate")
def unconsolidate_finding(workspace_id: str, finding_id: str):
    ws = _ws(workspace_id)
    item = findings.unconsolidate(ws, finding_id)
    return _finding_payload(ws, item)


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
