"""Carrying an exploratory procedure that found something into a Data Test.

Findings are drafted only from RCM test executions. A saved analysis is
computed in a parallel universe with no edge into the audit graph, so an
exploratory procedure that found a real exception expires where it ran. The
engagement this was written for lost an invoice of 80,000,000 billed against a
purchase order of 8,000,000 exactly that way: an analysis found it, the
planning memorandum named it the most significant analytic result of the
engagement, and no test was ever written for it.

The fix is smaller than it looks, because **the analysis is already an executed
procedure**. Its Polars source, for the ``python`` kind, is a valid Data Test
step as written — filtering a frame into ``result``. What it lacks is not
computation but audit framing: which control it is evidence about, what it is
called, and which population it asserts over. That is one model turn, and it is
a fitting turn rather than a generation turn.

Three properties this is built for:

* **Complete.** Every analysis holding exceptions is dispositioned — promoted
  or declined with a recorded reason. Nothing is filtered out beforehand on a
  guess about relevance, because the guess is the thing that loses items.
* **Idempotent.** A disposition is durable, so a second run asks only about
  analyses nobody has answered yet.
* **Declinable.** The three largest exception counts in the engagement this was
  written for were weekend approvals, weekend hire dates, and unusually large
  requisition values — calendar and distribution facts, not control exceptions.
  Deciding that a screen is not a control test is the auditor's judgement, and
  it belongs at this turn rather than in a filter upstream of it. A decline is
  recorded, never silent.

What it does **not** reach: a procedure that returned a clean result cannot be
promoted, so an analysis whose key was wrong reports coverage it does not have
and graduates nothing. The duplicate screen keyed on
``(VENDOR_ID, VENDOR_INVOICE_NUMBER)`` in that engagement cleared a risk it
could not see by construction, and no mechanism keyed on exception counts will
ever notice. That is a test-quality problem and is fixed where the key is
written, not here.

No flagged row is read anywhere in this module. The definition, the verdict and
the counts are what a fitting turn needs; the rows stay in their sidecar under
``allow_analysis_exception_rows``, which this path never requests.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .text import counted
from .workspaces import Workspace

#: Where a disposition is recorded on the analysis it answers.
PROMOTION_FIELD = "promotion"

PROMOTED = "promoted"
DECLINED = "declined"


def exception_count(analysis: Mapping[str, Any]) -> int:
    """How many rows the analysis' current result flagged, or zero."""
    result = analysis.get("last_result")
    if not isinstance(result, Mapping):
        return 0
    try:
        return max(0, int(result.get("exception_count") or 0))
    except (TypeError, ValueError):
        return 0


def disposition(analysis: Mapping[str, Any]) -> dict[str, Any] | None:
    """The recorded answer for this analysis, if one has been given.

    Stamped with the ``result_sha1`` it answered. A procedure that was declined
    and then rewritten until it found something else has not been answered —
    the disposition belonged to a conclusion that no longer exists, which is
    the same rule ``read_exception_evidence`` applies to the flagged rows.
    """
    recorded = analysis.get(PROMOTION_FIELD)
    if not isinstance(recorded, Mapping):
        return None
    result = analysis.get("last_result")
    current = (
        str(result.get("result_sha1") or "") if isinstance(result, Mapping) else ""
    )
    if str(recorded.get("result_sha1") or "") != current:
        return None
    return dict(recorded)


def candidates(workspace: Workspace) -> list[dict[str, Any]]:
    """Analyses holding exceptions that nobody has answered for yet.

    Ordered by exception count so that, under a run budget that cannot reach
    every one, the procedures that found the most go first. Deliberately not
    filtered by whether some test already covers the same columns: a filter
    that decides relevance without asking is the failure this capability
    exists to remove, and a redundant promotion is a duplicate test an auditor
    can delete, which is the cheaper error.
    """
    pending = [
        analysis
        for analysis in workspace.analyses
        if exception_count(analysis) > 0 and disposition(analysis) is None
    ]
    return sorted(
        pending,
        key=lambda analysis: (-exception_count(analysis), str(analysis.get("id") or "")),
    )


def carries_source(analysis: Mapping[str, Any]) -> str:
    """The analysis' own Polars source, for the kinds that have one.

    A ``python`` analysis states its procedure as code and that code is carried
    through verbatim: it is the procedure that actually ran and produced the
    exceptions being promoted, and a model asked to restate it can only make it
    different. An ``analytics`` analysis names a catalog procedure and its
    parameters instead, and has no source to carry — the fitting turn writes
    the equivalent step, which the catalog entry specifies exactly.
    """
    if str(analysis.get("kind") or "") != "python":
        return ""
    spec = analysis.get("spec")
    return str(spec.get("code") or "").strip() if isinstance(spec, Mapping) else ""


def fitting_subject(analysis: Mapping[str, Any]) -> dict[str, Any]:
    """What one fitting turn is told about the procedure it is placing.

    The definition and the outcome, never the flagged rows. A fitting turn
    decides which control the procedure is evidence about and how to state it
    as a test; it does not need to see what was flagged to do that, and the
    permission that would admit those rows is deliberately not on this path.
    """
    result = analysis.get("last_result")
    result = result if isinstance(result, Mapping) else {}
    spec = analysis.get("spec")
    spec = spec if isinstance(spec, Mapping) else {}
    return {
        "analysis_id": str(analysis.get("id") or ""),
        "title": str(analysis.get("title") or ""),
        # The authored rationale — why an auditor or the analysis worker ran
        # this at all. It is the closest thing the procedure has to a control
        # objective, and it is what makes the fit decidable.
        "note": str(analysis.get("note") or ""),
        "frame": str(analysis.get("table") or ""),
        "kind": str(analysis.get("kind") or ""),
        "catalog_test": spec.get("test"),
        "parameters": spec.get("params") or {},
        "code": carries_source(analysis),
        "verdict": result.get("verdict"),
        "verdict_text": result.get("verdict_text"),
        "exception_count": exception_count(analysis),
        "population": result.get("population"),
        "tested": result.get("tested"),
    }


def promoted_record(
    *,
    result_sha1: str,
    test_id: str,
    rcm_id: str,
    agent_run_id: str | None,
    decided_at: str,
) -> dict[str, Any]:
    """The disposition written when a procedure becomes a test."""
    return {
        "state": PROMOTED,
        "result_sha1": result_sha1,
        "test_id": test_id,
        "rcm_id": rcm_id,
        "agent_run_id": agent_run_id,
        "decided_at": decided_at,
    }


def declined_record(
    *,
    result_sha1: str,
    reason: str,
    agent_run_id: str | None,
    decided_at: str,
) -> dict[str, Any]:
    """The disposition written when a procedure is judged not a control test."""
    return {
        "state": DECLINED,
        "result_sha1": result_sha1,
        "reason": reason,
        "agent_run_id": agent_run_id,
        "decided_at": decided_at,
    }


def result_sha1(analysis: Mapping[str, Any]) -> str:
    result = analysis.get("last_result")
    return str(result.get("result_sha1") or "") if isinstance(result, Mapping) else ""


def declined(workspace: Workspace) -> list[dict[str, Any]]:
    """Every procedure that was answered by setting it aside, with its reason.

    Read by the run record and the coverage narrative. A decline is a judgement
    about the engagement — that a weekend-approval screen is a calendar fact
    rather than a control exception — and it is exactly as reportable as a
    promotion.
    """
    answers = []
    for analysis in workspace.analyses:
        recorded = disposition(analysis)
        if not recorded or recorded.get("state") != DECLINED:
            continue
        answers.append(
            {
                "analysis_id": str(analysis.get("id") or ""),
                "title": str(analysis.get("title") or ""),
                "exception_count": exception_count(analysis),
                "reason": str(recorded.get("reason") or ""),
            }
        )
    return answers


def undispositioned_warning(workspace: Workspace) -> str:
    """One sentence for procedures that found something and got no answer.

    The assertion behind the whole capability. If this is ever non-empty at the
    end of a run, an exploratory procedure computed exceptions that reached no
    test and no recorded decision — which is the failure being fixed, restated
    as something the run can say about itself.
    """
    pending = candidates(workspace)
    if not pending:
        return ""
    total = sum(exception_count(analysis) for analysis in pending)
    named = ", ".join(str(analysis.get("title") or "") for analysis in pending[:3])
    sentence = (
        f"{counted(len(pending), 'saved analysis', 'saved analyses')} holding "
        f"{counted(total, 'exception')} {'was' if len(pending) == 1 else 'were'} "
        f"neither promoted to a test nor recorded as declined: {named}"
    )
    if len(pending) > 3:
        sentence += f", and {counted(len(pending) - 3, 'other')}"
    return sentence + "."
