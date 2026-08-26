"""The engagement record: every work product this engagement filed, in the
order each one reached its current state.

The chat transcript is where a run is *watched*. It is keyed by conversation,
it scrolls away, and a milestone card in it answers "what did this run do"
rather than "what does this engagement have". This module is the other
projection of the same records, keyed by the work product instead of by the
conversation: what was filed, what produced it, what it cost, and — where a
step was attempted more than once — how many attempts that took.

Nothing here is a new record. A milestone is already the deterministic,
idempotent projection a workflow stage writes when it settles (see
`agent.narration.milestone`); this module groups those by the artifact they
filed and joins them to the run that emitted them.

The record also runs forward. A stage that has never produced its work product is drawn as an entry the ledger has not written yet, and a stage that did produce one but left something open carries that debt on its own row. Between them they answer the question the record could not: what should happen next.

Counts come from the workspace as it stands, not from the milestone that
happened to be last. A milestone's metrics mix engagement state ("RCM rows: 28")
with the delta for that one run ("Drafts prepared: 1"), and the final
`findings.drafted` milestone on a real engagement reads "1" against a register
holding thirty-five. The record answers what the engagement holds.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import doc_tests, engagement, rcm_execution, report
from .agent import store
from .agent.audit_execution import UNNARRATED_CAPABILITIES
from .workspaces import Workspace

# --------------------------------------------------------------------------- #
# What each capability files
# --------------------------------------------------------------------------- #
# `count` names a key in the tally built by `_counts`; None means the work
# product has no meaningful size to state. `destination` is the frontend
# navigation destination that opens it, kept in the vocabulary
# `useWorkspaceNavigation` already speaks.
_FILED: dict[str, dict[str, Any]] = {
    "documents.analysis_generated": {
        "label": "Document analyses", "destination": "documents",
        "unit": "document", "count": "documents",
    },
    "analysis.executed": {
        "label": "Analysis library", "destination": "analysis",
        "unit": "analysis", "unit_plural": "analyses", "count": "analyses",
    },
    "planning.apm_ready": {
        "label": "Audit planning memorandum", "destination": "apm",
        "unit": "", "count": None,
    },
    "planning.rcm_ready": {
        "label": "Risk and control matrix", "destination": "rcm",
        "unit": "row", "count": "rcm",
    },
    "tests.specified": {
        "label": "Test programme", "destination": "data-tests",
        "unit": "test", "count": "tests",
    },
    "doc_tests.executed": {
        "label": "Document test results", "destination": "doc-tests",
        "unit": "test", "count": "document_tests",
    },
    # Fieldwork schedules and rolls up tests another stage filed, so it has no
    # register of its own to size. Counting the document-test register here
    # claimed the same artifact twice, the second time with a number fieldwork
    # never produced.
    "fieldwork.executed": {
        "label": "Fieldwork results", "destination": "doc-tests",
        "unit": "", "count": None,
    },
    "results.rolled_up": {
        "label": "Control conclusions", "destination": "rcm",
        "unit": "row", "count": "rcm",
    },
    "findings.drafted": {
        "label": "Findings register", "destination": "findings",
        "unit": "finding", "count": "findings",
    },
    "report.working_draft": {
        "label": "Report", "destination": "report",
        "unit": "", "count": None,
    },
    "dashboard.curated": {
        "label": "Dashboard curation", "destination": "dashboard",
        "unit": "pinned item", "count": "tiles",
    },
    "audit.verified": {
        "label": "Verification", "destination": "dashboard",
        "unit": "", "count": None,
    },
}

# A run whose status is one of these stopped early, so the wall clock between
# its start and its milestone is not time the agent spent working.
_SETTLED = ("completed", "completed_with_issues", "completed_with_open_items",
            "completed_with_failures")


def _counts(workspace: Workspace) -> dict[str, int]:
    """What the engagement holds right now, per work product."""
    try:
        document_tests = len(doc_tests.list_tests(workspace))
    except Exception:
        document_tests = 0
    data_tests = len(workspace.data_tests)
    return {
        "documents": len(workspace.documents),
        "analyses": len(workspace.analyses),
        "rcm": len(workspace.rcm),
        "data_tests": data_tests,
        "document_tests": document_tests,
        "tests": data_tests + document_tests,
        "findings": len(workspace.findings),
        "tiles": len(workspace.tiles),
    }


def _parsed(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _gap_ms(start: object, end: object) -> int | None:
    """Milliseconds between two stored timestamps, when both parse."""
    first, last = _parsed(start), _parsed(end)
    if first is None or last is None:
        return None
    delta = int((last - first).total_seconds() * 1000)
    return delta if delta >= 0 else None


def _milestone_rows(run: dict) -> list[dict]:
    """One row per milestone on a run, each carrying what it cost.

    A stage's cost is the wall clock from the moment the previous stage in the
    same run settled — or from the run starting, for the first — to the moment
    this one did. Two stages that settle in the same second give the second one
    zero, which is the honest reading: they finished together.
    """
    rows = []
    previous = run.get("started") or run.get("created")
    for milestone in run.get("milestones") or []:
        if not isinstance(milestone, dict):
            continue
        settled_at = milestone.get("created_at")
        elapsed = _gap_ms(previous, settled_at) if run.get("status") in _SETTLED else None
        rows.append({
            "milestone": milestone,
            "run_id": str(run.get("id") or ""),
            "run_status": str(run.get("status") or ""),
            "chat_id": str(run.get("chat_id") or "") or None,
            "objective": str((run.get("route") or {}).get("objective") or "").strip(),
            "at": settled_at,
            "elapsed_ms": elapsed,
        })
        previous = settled_at or previous
    return rows


def _entry(capability: str, rows: list[dict], counts: dict[str, int]) -> dict:
    """Collapse every attempt at one capability into a single filed entry.

    The narrative comes from the latest attempt because that is the state the
    engagement is actually in. The cost is the sum of every attempt, because
    three tries at the RCM cost what three tries cost.
    """
    rows = sorted(rows, key=lambda row: str(row["at"] or ""))
    latest = rows[-1]
    milestone = latest["milestone"]
    filed = _FILED.get(capability)

    measured = [row["elapsed_ms"] for row in rows if row["elapsed_ms"] is not None]
    count_key = (filed or {}).get("count")
    return {
        "id": f"{capability}:{milestone.get('id') or latest['run_id']}",
        "capability": capability,
        "at": latest["at"],
        "first_at": rows[0]["at"],
        "status": str(milestone.get("status") or ""),
        "headline": str(milestone.get("headline") or ""),
        "summary": str(milestone.get("summary") or ""),
        "metrics": list(milestone.get("metrics") or []),
        "highlights": list(milestone.get("highlights") or []),
        "objective": latest["objective"],
        "run_id": latest["run_id"],
        "chat_id": latest["chat_id"],
        # Every run that contributed, newest last, so a reader can open the
        # attempts a collapsed row is standing in for.
        "attempts": [
            {
                "run_id": row["run_id"],
                "run_status": row["run_status"],
                "at": row["at"],
                "elapsed_ms": row["elapsed_ms"],
            }
            for row in rows
        ],
        "elapsed_ms": sum(measured) if measured else None,
        # A cancelled or failed run's wall clock counts however long it sat
        # waiting for a person, so those attempts are not timed. When this is
        # short of `attempts`, the elapsed figure covers only part of the work
        # and a reader who is being sold time saved needs to know which part.
        "measured_attempts": len(measured),
        "filed": None if filed is None else {
            "label": filed["label"],
            "destination": filed["destination"],
            "unit": filed["unit"],
            # Irregular plurals are declared beside the unit rather than left
            # to the caller, which produced "28 analysiss".
            "unit_plural": filed.get("unit_plural") or (
                f"{filed['unit']}s" if filed["unit"] else ""
            ),
            "count": counts.get(count_key) if count_key else None,
        },
    }


# --------------------------------------------------------------------------- #
# What has not run yet
# --------------------------------------------------------------------------- #
# A stage earns a place on the tail only when its work product is genuinely
# absent from the workspace. "No milestone" is emphatically not "not done":
# `report.working_draft` files no milestone on this repo's demo engagement while
# the workspace holds 78,000 characters of report, and nine stages
# (`UNNARRATED_CAPABILITIES`) never narrate at all by design. Diffing the plan
# against the milestones would advertise both as work still owed.
#
# `present` therefore tests the artifact, which is the same rule the filed rows
# already use for their counts, so both halves of the ledger agree about what
# exists. A stage with no cheap presence test is simply left off — `audit.verified`
# is read-only and commits nothing, so its absence is not observable and it is
# never drawn.


def _report_markdown(workspace: Workspace) -> str:
    """Deliberately does not swallow: a presence test that cannot answer must
    raise, so `_pending` skips the stage rather than reporting it as absent and
    inviting the reader to redo work that may already exist."""
    return str((report.hydrate(workspace) or {}).get("markdown") or "")


_PHANTOM: dict[str, dict[str, Any]] = {
    "planning.apm_ready": {
        "present": lambda ws: bool(str((ws.planning or {}).get("apm_markdown") or "").strip()),
        "headline": "Draft the audit planning memorandum",
        "prompt": "Draft the APM.",
    },
    "planning.rcm_ready": {
        "present": lambda ws: bool(ws.rcm),
        "headline": "Build the risk and control matrix",
        "prompt": "Generate the RCM.",
        "needs": ("planning.apm_ready", "the memorandum"),
    },
    "tests.specified": {
        "present": lambda ws: bool(ws.data_tests) or bool(_document_tests(ws)),
        "headline": "Specify the tests each control needs",
        "prompt": "Draft the tests the RCM rows still need.",
        "needs": ("planning.rcm_ready", "the matrix"),
    },
    "findings.drafted": {
        "present": lambda ws: bool(ws.findings),
        "headline": "Draft findings from the exceptions",
        "prompt": "Draft findings.",
        "needs": ("tests.specified", "the tests"),
    },
    "dashboard.curated": {
        "present": lambda ws: bool(ws.tiles),
        "headline": "Pick the analyses worth showing on the dashboard",
        "prompt": "Curate the dashboard.",
        "needs": ("analysis", "the analysis library"),
    },
    "report.working_draft": {
        "present": lambda ws: bool(_report_markdown(ws).strip()),
        "headline": "Write the report from the findings",
        "prompt": "Generate the report.",
        "needs": ("findings.drafted", "the findings"),
    },
}


def _document_tests(workspace: Workspace) -> list[dict]:
    try:
        return list(doc_tests.list_tests(workspace))
    except Exception:
        return []


def _plan_order() -> dict[str, int]:
    """Stage order as the engagement plan declares it."""
    try:
        outcomes = engagement.plan_outcomes(engagement.DEFAULT_TEMPLATE)
    except Exception:
        return {}
    return {str(item.get("capability") or ""): index for index, item in enumerate(outcomes)}


def _blocked_by(workspace: Workspace, spec: dict) -> str:
    """Why a stage cannot start, or '' when nothing holds it."""
    needs = spec.get("needs")
    if not needs:
        return ""
    dependency, noun = needs
    if dependency == "analysis":
        return "" if workspace.analyses else f"Waits for {noun}."
    upstream = _PHANTOM.get(dependency)
    if upstream and not upstream["present"](workspace):
        return f"Waits for {noun}."
    return ""


def _pending(workspace: Workspace) -> list[dict]:
    """Stages whose work product does not exist, in plan order."""
    order = _plan_order()
    rows = []
    for capability, spec in _PHANTOM.items():
        if capability in UNNARRATED_CAPABILITIES:
            continue
        try:
            if spec["present"](workspace):
                continue
        except Exception:
            # A presence test that cannot answer must not invent absent work.
            continue
        filed = _FILED.get(capability) or {}
        blocked = _blocked_by(workspace, spec)
        rows.append({
            "id": f"pending:{capability}",
            "capability": capability,
            "headline": spec["headline"],
            "blocked_reason": blocked,
            "runnable": not blocked,
            "start": {"prompt": spec["prompt"], "outcomes": [capability]},
            "filed": {
                "label": filed.get("label") or capability,
                "destination": filed.get("destination") or "",
                "unit": "", "unit_plural": "", "count": None,
            },
            "order": order.get(capability, len(order)),
        })
    return sorted(rows, key=lambda row: row["order"])


# --------------------------------------------------------------------------- #
# What a filed stage left open
# --------------------------------------------------------------------------- #
# Rank is deliberate, and it puts review ahead of unstarted work. Running the
# next stage is something the agent does by itself in auto mode; reading what it
# decided is the one thing only a person can do, so that is what the record asks
# for first.
_OPEN_RANK = {"unread_conclusions": 10, "findings_followup": 20, "draft_rcm": 30}


def _open_points(workspace: Workspace) -> list[dict]:
    """Debts left behind by stages that completed."""
    points: list[dict] = []

    try:
        completion = rcm_execution.completion(workspace)
        unread = len(completion.get("unreviewed_agent_conclusions") or [])
        linked = _linked_test_count(workspace)
    except Exception:
        unread, linked = 0, 0
    if unread:
        points.append({
            "key": "unread_conclusions",
            "capability": "results.rolled_up",
            "message": (
                f"{unread} of {linked} conclusions were set by the assistant "
                "and never read." if linked
                else f"{unread} conclusions were set by the assistant and never read."
            ),
            "action": "Open them",
            "destination": "rcm",
        })

    owed = [
        item for item in workspace.findings
        if item.get("cause_pending")
        or not str(item.get("management_response") or "").strip()
    ]
    if owed:
        points.append({
            "key": "findings_followup",
            "capability": "findings.drafted",
            "message": (
                f"{len(owed)} of {len(workspace.findings)} findings have no "
                "root cause or management response."
            ),
            "action": "Add causes",
            "destination": "findings",
        })

    draft = [row for row in workspace.rcm if str(row.get("review_status") or "") == "draft"]
    if draft:
        points.append({
            "key": "draft_rcm",
            "capability": "planning.rcm_ready",
            "message": (
                f"{len(draft)} of {len(workspace.rcm)} rows are still marked "
                "draft. None has been reviewed."
            ),
            "action": "Review rows",
            "destination": "rcm",
        })

    return sorted(points, key=lambda point: _OPEN_RANK.get(point["key"], 99))


def _linked_test_count(workspace: Workspace) -> int:
    """Tests attached to a row of the matrix.

    The same rule the dashboard's `tests_linked` uses, rather than every test in
    the workspace — a test with no `rcm_id` is not part of the population the
    unread-conclusion disclosure is a fraction of, and the two figures appear on
    screens one click apart.
    """
    rows = {str(row.get("id") or "") for row in workspace.rcm}
    return sum(
        1 for item in (*workspace.data_tests, *_document_tests(workspace))
        if str(item.get("rcm_id") or "") in rows
    )


def record(workspace: Workspace) -> dict:
    """Every work product the engagement filed, oldest settlement first."""
    runs = store.list_runs(workspace)
    counts = _counts(workspace)

    by_capability: dict[str, list[dict]] = {}
    contributing: set[str] = set()
    for summary in runs:
        # `list_runs` drops the milestone payload, so the full record is the
        # only place the briefings live.
        try:
            run = store.load_run(workspace, str(summary.get("id") or ""))
        except Exception:
            continue
        for row in _milestone_rows(run):
            capability = str(row["milestone"].get("capability") or "").strip()
            if not capability:
                continue
            by_capability.setdefault(capability, []).append(row)
            contributing.add(row["run_id"])

    entries = sorted(
        (_entry(capability, rows, counts) for capability, rows in by_capability.items()),
        key=lambda entry: str(entry["at"] or ""),
    )
    points = _open_points(workspace)
    by_capability_point: dict[str, list[dict]] = {}
    for point in points:
        by_capability_point.setdefault(point["capability"], []).append(point)
    for entry in entries:
        entry["open_points"] = by_capability_point.get(entry["capability"], [])
    # A debt whose stage is not on the record still has to be said; it hangs at
    # the end rather than disappearing with the row it expected to find.
    attached = {point["key"] for entry in entries for point in entry["open_points"]}
    orphaned = [point for point in points if point["key"] not in attached]

    pending = _pending(workspace)
    # Review outranks unstarted work — see `_OPEN_RANK`.
    first_runnable = next((row for row in pending if row["runnable"]), None)
    upcoming = points[0] if points else None
    next_step = (
        {"kind": "open_point", **upcoming} if upcoming
        else {"kind": "stage", **first_runnable} if first_runnable
        else None
    )

    measured = [entry["elapsed_ms"] for entry in entries if entry["elapsed_ms"] is not None]
    return {
        "entries": entries,
        "pending": pending,
        "open_points": points,
        "orphaned_points": orphaned,
        "next": next_step,
        "counts": counts,
        "totals": {
            "work_products": len(entries),
            "runs": len(runs),
            # A run that committed nothing filed nothing; saying so is more
            # honest than a record that silently drops a third of the history.
            "runs_that_filed": len(contributing),
            "attempts": sum(len(entry["attempts"]) for entry in entries),
            "elapsed_ms": sum(measured) if measured else None,
            "first_at": entries[0]["first_at"] if entries else None,
            "last_at": entries[-1]["at"] if entries else None,
        },
    }
