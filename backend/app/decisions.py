"""One queue for everything in an engagement that is waiting on a person.

Pending human work is produced by six independent parts of the system, and
before this module the auditor had to visit five screens to find it — the
dashboard said "10 need attention", document tests said "3 need review", the
RCM printed DRAFT seventeen times, findings had its own dispositions, and
approvals lived in the assistant drawer. None of them could answer the only
question that matters while the agent is running: *what does it need from me?*

This module answers that. It reads the same records those screens read and
normalizes them into one ordered list. It owns no state and mutates nothing:
resolving a decision still goes to the endpoint that already owned it, so the
queue can never drift from the surface it summarizes.
"""

from __future__ import annotations

from typing import Any

from . import dashboard, doc_tests, rcm_execution
from .agent import narration, store
from .agent.workflows import audit as audit_workflow
from .workspaces import Workspace, WorkspaceError

# Ordered worst-first. Severity decides colour and, after the unblock count,
# position: an auditor clearing a queue should meet the costly things first.
SEVERITIES = ("critical", "warning", "info")
_SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES)}

KINDS = (
    "approval",
    "interaction",
    "blocker",
    "doc_test_item",
    "observation",
    "quality",
)

# Dashboard attention is the one source that overlaps the others, in two ways.
# Its `doctest:` rows restate document-test items this queue already carries at
# item granularity, so the whole prefix is dropped.
_ATTENTION_PREFIXES = ("table:", "quality:", "tile:")

# And one report-quality check restates the queue's own rows: `unresolved_exception`
# fires once per open observation and once per document test whose exceptions
# have no RCM observation — both of which arrive here from their own sources,
# closer to the work. Every other quality code is about the report itself and
# has no other home.
_DUPLICATED_QUALITY_CODES = frozenset({"unresolved_exception"})

# Workspace-level work is not owned by a run, but it still sits at a known point
# in the audit graph, which is what makes an unblock count computable for it.
_CAPABILITY_BY_KIND = {
    "doc_test_item": "fieldwork.executed",
    "observation": "results.rolled_up",
    "quality": "report.working_draft",
}

_DOC_TEST_ACTION_CLASSES = {
    "exception": ("critical", "Exception to disposition"),
    "needs_review": ("warning", "Awaiting your review"),
    "awaiting_evidence": ("warning", "Awaiting evidence"),
}


def _target(tab: str, **query: str) -> dict[str, Any]:
    return {"tab": tab, "query": {key: value for key, value in query.items() if value}}


def quality_code(attention_id: str) -> str:
    """Extract the report-quality code from a `quality:<code>:<ordinal>` id."""
    parts = str(attention_id).split(":")
    return parts[1] if len(parts) > 1 else ""


def _clip(text: str, limit: int = 96) -> str:
    """A queue row is scanned, not read; the full text stays in `context`."""
    value = " ".join(str(text).split())
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _capability_for_stage(run: dict, stage_id: str) -> str:
    for stage in (run.get("workflow") or {}).get("stages") or []:
        if str(stage.get("id")) == str(stage_id):
            return str(stage.get("capability") or "")
    return ""


def _capability_for_task(run: dict, task_id: str) -> str:
    """Approvals point at a plan task; the task names the stage that owns it."""
    for stage in (run.get("plan") or {}).get("stages") or []:
        for task in stage.get("tasks") or []:
            if str(task.get("id")) == str(task_id):
                return _capability_for_stage(run, str(task.get("stage") or stage.get("id")))
    return ""


def _unblocks(capability_id: str) -> dict[str, Any]:
    """What resolving this releases, named directly and counted in full."""
    if not capability_id or capability_id not in audit_workflow.DEPENDENCIES:
        return {"capability": capability_id, "next": [], "downstream": 0}
    return {
        "capability": capability_id,
        "next": list(audit_workflow.unblocked_by(capability_id)),
        "downstream": len(audit_workflow.downstream_of(capability_id)),
    }


def _item(
    *,
    id: str,
    kind: str,
    severity: str,
    title: str,
    context: str,
    created_at: str | None,
    target: dict[str, Any],
    capability: str = "",
    source_ref: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": id,
        "kind": kind,
        "severity": severity if severity in _SEVERITY_RANK else "info",
        "title": title,
        "context": context,
        "created_at": created_at,
        "target": target,
        "unblocks": _unblocks(capability),
        "source_ref": source_ref or {},
    }


# --------------------------------------------------------------------------- #
# Sources
# --------------------------------------------------------------------------- #
def _from_run(run: dict) -> list[dict[str, Any]]:
    """Approvals, interactions, and stopped units from the current run."""
    items: list[dict[str, Any]] = []
    run_id = str(run.get("id") or "")

    for approval in run.get("approvals") or []:
        if approval.get("status") != "pending":
            continue
        count = len(approval.get("items") or [])
        kind = str(approval.get("kind") or "change").replace("_", " ")
        items.append(_item(
            id=f"approval:{run_id}:{approval.get('id')}",
            kind="approval",
            severity="warning",
            title=f"Approve {count} proposed {kind} change{'' if count == 1 else 's'}",
            context="The agent is holding these until you decide.",
            created_at=approval.get("created"),
            target=_target("console"),
            capability=_capability_for_task(run, str(approval.get("task_id") or "")),
            source_ref={"run_id": run_id, "approval_id": approval.get("id")},
        ))

    for interaction in run.get("interactions") or []:
        if interaction.get("status") != "pending":
            continue
        items.append(_item(
            id=f"interaction:{run_id}:{interaction.get('id')}",
            kind="interaction",
            severity="warning",
            title=str(interaction.get("prompt") or "The agent needs an answer"),
            context=str(interaction.get("policy_reason") or ""),
            created_at=interaction.get("created_at"),
            target=_target("console"),
            source_ref={"run_id": run_id, "interaction_id": interaction.get("id")},
        ))

    # A unit that stopped needing a person is pending work even when it never
    # became a typed interaction — that gap is why blocked runs used to end
    # quietly, with the reason buried inside a collapsed card.
    for blocker in narration.blockers(run):
        severity = "critical" if blocker.get("severity") == "failed" else "warning"
        unit_ids = list(blocker.get("unit_ids") or [])
        # Several units of the same stage stop for different reasons, so the
        # stage title alone produces a queue of identical rows. The subject is
        # what distinguishes them; the stage says which step they belong to.
        subject = str(blocker.get("subject") or "").strip()
        stage_title = str(blocker.get("stage_title") or "Work stopped")
        items.append(_item(
            # An uncatalogued failure puts the raw provider error in `code`, so
            # the unit id is the only stable identity available here.
            id=f"blocker:{run_id}:{unit_ids[0] if unit_ids else blocker.get('unit_id')}",
            kind="blocker",
            severity=severity,
            title=_clip(subject) if subject else stage_title,
            context=str(blocker.get("message") or ""),
            created_at=run.get("started") or run.get("created"),
            target=_target(str(blocker.get("where") or "console")),
            capability=_capability_for_stage(run, str(blocker.get("stage_id") or "")),
            source_ref={
                "run_id": run_id,
                "stage_title": stage_title,
                "unit_ids": unit_ids,
                "suggestions": list(blocker.get("suggestions") or []),
            },
        ))
    return items


def _from_doc_tests(workspace: Workspace) -> list[dict[str, Any]]:
    payload = doc_tests.summary_payload(workspace)
    items: list[dict[str, Any]] = []
    for entry in payload.get("items") or []:
        mapped = _DOC_TEST_ACTION_CLASSES.get(str(entry.get("classification")))
        if not mapped:
            continue
        severity, context = mapped
        items.append(_item(
            id=f"doctest:{entry.get('item_id')}",
            kind="doc_test_item",
            severity=severity,
            title=str(entry.get("label") or entry.get("test_title") or "Document test item"),
            context=f"{context} · {entry.get('test_title') or ''}".strip(" ·"),
            created_at=entry.get("updated"),
            target=_target(
                "doc-tests",
                test=str(entry.get("test_id") or ""),
                item=str(entry.get("item_id") or ""),
            ),
            capability=_CAPABILITY_BY_KIND["doc_test_item"],
            source_ref={
                "test_id": entry.get("test_id"),
                "item_id": entry.get("item_id"),
                "rcm_id": entry.get("rcm_id"),
            },
        ))
    return items


def _from_observations(workspace: Workspace) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for observation in workspace.observations:
        if observation.get("status") == "disposed":
            continue
        exceptions = int(observation.get("exception_count") or 0)
        items.append(_item(
            id=f"observation:{observation.get('id')}",
            kind="observation",
            severity="critical" if exceptions else "warning",
            title=str(observation.get("summary") or "Execution observation"),
            context=(
                f"{exceptions} exception(s) — disposition required"
                if exceptions else "Disposition required"
            ),
            created_at=observation.get("created"),
            target=_target(
                "rcm",
                rcm=str(observation.get("rcm_id") or ""),
                observation=str(observation.get("id") or ""),
            ),
            capability=_CAPABILITY_BY_KIND["observation"],
            source_ref={
                "observation_id": observation.get("id"),
                "rcm_id": observation.get("rcm_id"),
                "test_id": observation.get("test_id"),
            },
        ))
    return items


def _from_attention(workspace: Workspace) -> list[dict[str, Any]]:
    payload = dashboard.dashboard_payload(workspace)
    items: list[dict[str, Any]] = []
    for entry in payload.get("attention") or []:
        entry_id = str(entry.get("id") or "")
        if not entry_id.startswith(_ATTENTION_PREFIXES):
            continue
        # Attention ids are `quality:<code>:<ordinal>`; the code decides whether
        # another source already owns this row.
        if entry_id.startswith("quality:") and quality_code(entry_id) in _DUPLICATED_QUALITY_CODES:
            continue
        items.append(_item(
            id=f"quality:{entry_id}",
            kind="quality",
            severity="critical" if entry.get("severity") == "error" else "warning",
            title=str(entry.get("title") or "Needs attention"),
            context=str(entry.get("message") or ""),
            created_at=None,
            target=entry.get("target") or _target("dashboard"),
            capability=_CAPABILITY_BY_KIND["quality"] if entry_id.startswith("quality:") else "",
            source_ref={"attention_id": entry_id},
        ))
    return items


# --------------------------------------------------------------------------- #
# Payload
# --------------------------------------------------------------------------- #
def _sort_key(item: dict[str, Any]) -> tuple[int, int, str]:
    # Most severe first, then whatever unblocks the most work, then oldest —
    # so clearing the queue top-down releases the most downstream work soonest.
    return (
        _SEVERITY_RANK.get(item["severity"], len(SEVERITIES)),
        -int(item["unblocks"]["downstream"]),
        str(item.get("created_at") or ""),
    )


def _current_run(workspace: Workspace) -> dict | None:
    """The run that owns pending human work.

    The live run when there is one, otherwise the most recent — an interrupted
    or failed run still holds blockers the auditor has to clear before anything
    can resume, and those are exactly the ones nothing else surfaces.
    """
    summaries = store.list_runs(workspace)
    if not summaries:
        return None
    resting = set(store.ACTIVE_STATUSES) | set(store.RESUMABLE_STATUSES)
    chosen = next(
        (item for item in summaries if item.get("status") in resting),
        summaries[0],
    )
    try:
        return store.load_run(workspace, str(chosen["id"]))
    except WorkspaceError:
        return None


def decisions_payload(workspace: Workspace) -> dict[str, Any]:
    """Everything waiting on the auditor, worst and most-blocking first."""
    run = _current_run(workspace)
    items = [
        *(_from_run(run) if run else []),
        *_from_doc_tests(workspace),
        *_from_observations(workspace),
        *_from_attention(workspace),
    ]
    items.sort(key=_sort_key)

    by_kind = {kind: 0 for kind in KINDS}
    by_severity = {name: 0 for name in SEVERITIES}
    for item in items:
        by_kind[item["kind"]] = by_kind.get(item["kind"], 0) + 1
        by_severity[item["severity"]] += 1

    return {
        "items": items,
        "total": len(items),
        "by_kind": by_kind,
        "by_severity": by_severity,
        # The run is what a decision usually unblocks, so the queue states
        # plainly whether the agent is sitting waiting on this list.
        "run": {
            "id": str(run.get("id")) if run else "",
            "status": str(run.get("status") or "") if run else "",
            "waiting": bool(run and run.get("status") in {"awaiting_approval", "awaiting_input"}),
        },
    }
