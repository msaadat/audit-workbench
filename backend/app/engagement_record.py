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

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from typing import Any

from . import (
    doc_tests,
    document_classification,
    documents as document_service,
    engagement,
    rcm_execution,
    report,
)
from .agent import capabilities as audit_capabilities
from .agent import store
from .agent.capabilities.documents import has_generated_analysis
from .agent.workflows import audit as audit_workflow
from .agent.workflows import documents as documents_workflow
from .workspaces import Workspace

# --------------------------------------------------------------------------- #
# What the record draws, and what the graph already knows
# --------------------------------------------------------------------------- #
# The spine is the audit graph. `agent.workflows.audit` owns which stage waits
# for which, and every capability module owns a `readiness` function that tests
# its own output and says in a sentence what is left. Order, blocking, and that
# sentence are read from there rather than restated here: the restatement
# drifted once already, and `_blocked_by` records what that cost.
#
# What stays declared is what the graph has no opinion about. The graph's own
# titles name the capability — "Eligible finding drafts", "Executable test
# specifications" — which is what *runs*; an auditor opens a findings register
# and a test programme. Membership is declared for the same reason: document
# tests are their own workflow rather than a step of the audit plan, so no
# single closure contains everything the record should draw.
#
# `count` names a key in the tally built by `_counts`; None means the work
# product has no meaningful size to state. `destination` is the frontend
# navigation destination that opens it, kept in the vocabulary
# `useWorkspaceNavigation` already speaks. `headline` and `prompt` are the
# imperative a stage carries while its work product does not exist yet.
#
# `live_body` marks a stage whose milestone projection describes the workspace
# rather than the run that wrote it, so the record can recompute it — see
# `_live_bodies`. Not every projection does: some count the units a run
# scheduled or the rows it changed, and re-run with no run to describe those
# report zero. That is the test, and it is a cheap one — a delta over an empty
# run reads "0 scheduled tests" and "no new drafts", where a figure read from
# the workspace stays 22 rows, 54 tests, 30 findings.
_SPINE: dict[str, dict[str, Any]] = {
    # The head of the chain, and the only row the agent does not produce. Its
    # card opens nothing because there is no one Sources page: the two doors
    # below it go to the two catalogues the engagement actually keeps. When it
    # is empty it asks for an import rather than offering to run something,
    # which is what `action` is for — the assistant cannot import.
    "sources.imported": {
        "label": "Sources", "destination": "",
        "unit": "", "count": None,
        "headline": "Bring in the audit file",
        "action": "import",
        "links": (
            {"label": "Documents", "destination": "documents", "count": "documents"},
            {"label": "Tables", "destination": "data", "count": "tables"},
        ),
    },
    # Sized by the analyses that exist, never by the documents they would be
    # written about. Counting `documents` here read the import as the work:
    # eight files landed and the row drew "Document analyses — 8" in the colour
    # reserved for filed work, on an engagement whose own readiness block on the
    # same row said "8 documents have no generated analysis". A stage is held
    # when its work product exists, and an unanalysed document is not one.
    "documents.analysis_generated": {
        "label": "Document analyses", "destination": "documents",
        "unit": "document", "count": "document_analyses",
        "headline": "Analyse the imported documents",
        "prompt": "Analyse the documents.",
        # The documents graph forks at `documents.categorized`: prose ends here,
        # transaction evidence ends at `documents.schemas_stamped`, and the two
        # never reconverge. The row key alone closes over the prose branch and
        # reports satisfied over exactly the half it asked for — nine documents
        # classified on the treasury engagement, eight of them evidence, one
        # analysis filed, run completed with no warning.
        "outcomes": list(documents_workflow.FULL_DOCUMENT_OUTCOMES),
        # The evidence read is sequential, so a large corpus is a long run and
        # deferring it is a real thing to want. `planning.rcm_ready` depends on
        # `documents.schemas_stamped`, so the reading is still owed either way —
        # which is what the note has to say.
        "alternates": (
            {
                "label": "Planning documents only",
                "prompt": "Analyse the planning documents.",
                "outcomes": ["documents.analysis_generated"],
                "note": (
                    "Leaves transaction evidence unread — the RCM will read it "
                    "later."
                ),
            },
        ),
    },
    "analysis.executed": {
        "label": "Analysis library", "destination": "analysis",
        "unit": "analysis", "unit_plural": "analyses", "count": "analyses",
        "headline": "Analyse the imported tables",
        "prompt": "Analyse the imported tables.",
        # The one row whose button asks for more than the row is sized by. The
        # stage is held when the library exists, so `analyses` is what counts
        # it; but "analyse the imported tables" is answered by what the analysis
        # *found*, and the workflow's terminus for that request is the memo —
        # see `FULL_ANALYSIS_OUTCOMES`. Requesting the row key alone stopped the
        # run one stage short of the write-up, with nothing on the record to say
        # a summary was still owed.
        "outcomes": ["analysis.summarized"],
        # The bench beside the library. A query is not an artifact the
        # engagement holds — nothing is filed by running one — so it is marked
        # a tool and drawn as one, or the record would start claiming the
        # engagement holds a query.
        "links": ({"label": "Query", "destination": "query", "kind": "tool"},),
    },
    "planning.apm_ready": {
        "label": "Audit planning memorandum", "destination": "apm",
        "unit": "", "count": None,
        "headline": "Draft the audit planning memorandum",
        "prompt": "Draft the APM.", "live_body": True,
    },
    "planning.rcm_ready": {
        "label": "Risk and control matrix", "destination": "rcm",
        "unit": "row", "count": "rcm",
        "headline": "Build the risk and control matrix",
        "prompt": "Generate the RCM.", "live_body": True,
    },
    "tests.specified": {
        "label": "Test programme", "destination": "data-tests",
        "unit": "test", "count": "tests",
        "headline": "Specify the tests each control needs",
        "prompt": "Draft the tests the RCM rows still need.", "live_body": True,
    },
    # Held by the results its register carries, never by the register's size —
    # see `_doc_tests_ran`. `count` still names the whole register, which is
    # what the pill shows once the row is held; an unheld row shows "not yet"
    # rather than a number, so the specified count is never read as a result
    # count.
    #
    # No `headline`, so the row is never drawn as work to start: the button that
    # runs these lives on `fieldwork.executed` directly below, whose prompt
    # covers the data and document registers together. The row still crosses to
    # the owed side of the ledger when nothing has run, which is what says the
    # results are missing.
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
        "headline": "Run the tests against the data and documents",
        "prompt": "Run the tests.",
    },
    "results.rolled_up": {
        "label": "Control conclusions", "destination": "rcm",
        "unit": "row", "count": "rcm",
        "headline": "Roll the results up into control conclusions",
        "prompt": "Roll the test results up into control conclusions.",
        "live_body": True,
    },
    "findings.drafted": {
        "label": "Findings register", "destination": "findings",
        "unit": "finding", "count": "findings",
        "headline": "Draft findings from the exceptions",
        "prompt": "Draft findings.",
    },
    "report.working_draft": {
        "label": "Report", "destination": "report",
        "unit": "", "count": None,
        "headline": "Write the report from the findings",
        "prompt": "Generate the report.", "live_body": True,
    },
    # Read-only: verification commits nothing, so there is no artifact whose
    # absence a reader could act on. It stays on the record as a row history can
    # attach to, and is never drawn as work the engagement owes. Its destination
    # is empty because there is genuinely nothing to open — it used to point at
    # the dashboard, which was neither its output nor anywhere it was explained.
    "audit.verified": {
        "label": "Verification", "destination": "",
        "unit": "", "count": None,
    },
}

# A run whose status is one of these stopped early, so the wall clock between
# its start and its milestone is not time the agent spent working.
_SETTLED = ("completed", "completed_with_issues", "completed_with_open_items",
            "completed_with_failures")


def _counts(workspace: Workspace) -> dict[str, int]:
    """What the engagement holds right now, per work product."""
    document_tests = len(_document_tests(workspace))
    data_tests = len(workspace.data_tests)
    return {
        "documents": len(workspace.documents),
        "document_analyses": _document_analyses(workspace),
        "tables": len(workspace.table_names()),
        "analyses": len(workspace.analyses),
        "rcm": len(workspace.rcm),
        "data_tests": data_tests,
        "document_tests": document_tests,
        "tests": data_tests + document_tests,
        "findings": len(workspace.findings),
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


def _history(capability: str, rows: list[dict]) -> dict:
    """What the runs behind one stage recorded, collapsed into one block.

    Layered onto a row that already stands without it. Cost, attempts, the
    milestone's own narrative and provenance are the things a run uniquely
    owns — none of them is derivable from the workspace — so they are carried
    here and are simply absent on a stage no run ever filed.

    The narrative comes from the latest attempt because that is the state the
    engagement is actually in. The cost is the sum of every attempt, because
    three tries at the RCM cost what three tries cost.
    """
    rows = sorted(rows, key=lambda row: str(row["at"] or ""))
    latest = rows[-1]
    milestone = latest["milestone"]

    measured = [row["elapsed_ms"] for row in rows if row["elapsed_ms"] is not None]
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
        # Absent on every stage whose result is not a distribution, and on
        # milestones filed before the channel existed.
        "stats": list(milestone.get("stats") or []),
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
    }


# --------------------------------------------------------------------------- #
# What the engagement holds
# --------------------------------------------------------------------------- #
# Whether a row is owed is decided by the artifact, never by the run history.
# "No milestone" is emphatically not "not done": `report.working_draft` files no
# milestone on this repo's demo engagement while the workspace holds 78,000
# characters of report, and nine stages never narrate at all by design. Diffing
# the plan against the milestones would advertise both as work still owed — and
# on an engagement whose run folder has been lost, would advertise all of it.


def _report_markdown(workspace: Workspace) -> str:
    """Deliberately does not swallow: a presence test that cannot answer must
    raise, so `_holds` reads the stage as absent only when it really is, rather
    than inviting the reader to redo work that may already exist."""
    return str((report.hydrate(workspace) or {}).get("markdown") or "")


# --------------------------------------------------------------------------- #
# One call, one read
# --------------------------------------------------------------------------- #
# The presence tests are pure reads of the workspace as it stands, and the
# record asks several of them several times: `_pending` tests every stage, then
# `_blocked_by` re-tests each one's dependencies, and `_open_points` reads the
# roll-up again for the unread-conclusion debt. Nothing recorded that these had
# already been answered, so a six-table engagement with sixteen document tests
# rebuilt the cycle-item projection seventy times and re-validated the same
# evidence records thirty-eight thousand times — sixteen seconds to draw a
# record whose inputs never changed while it was being drawn.
#
# The two reads that cost anything are memoized for the life of one `record()`
# call and no longer: both read files outside the manifest, so a cache that
# outlived the call would answer for a workspace that has since been written
# to. This is the same bounded-lifetime argument `Workspace._table_signature_cache`
# makes, taken one scope tighter because a run mutates doc tests through an
# instance it keeps.
_MEMO: ContextVar[dict[str, Any] | None] = ContextVar("record_memo", default=None)


@contextmanager
def _one_read():
    """One pass over the workspace, reading each expensive thing once.

    The record's own memo answers repeated questions about the same tally.
    Underneath it, ``doc_tests.request_cache_scope`` is what makes reading
    capability readiness affordable at all: `_findings_ready` asks
    `support_issues` about every finding, and each of those resolves the known
    test ids by listing every document test, which materializes every cycle
    item from its evidence records. Thirty findings paid for that thirty times.
    Inside the scope the whole readiness projection costs 123ms rather than
    2131ms, measured on a 30-finding engagement.

    Documents need the same treatment for the same reason. Readiness sweeps
    every document once per capability, and each sweep re-read that document's
    classification sidecar and its cached extraction: 1,936 sidecar reads and
    2,112 extraction reads for 88 documents on one engagement. Both scopes hold
    only for this call, so a run that writes an assignment still sees it.
    """
    token = _MEMO.set({})
    try:
        with (
            doc_tests.request_cache_scope(),
            document_classification.request_cache_scope(),
            document_service.request_cache_scope(),
        ):
            yield
    finally:
        _MEMO.reset(token)


def _once(key: str, compute):
    """``compute()``'s value, computed at most once inside :func:`_one_read`.

    Outside one, every call recomputes: a stale answer is worse than a slow
    one, and nothing but the record needs this.
    """
    memo = _MEMO.get()
    if memo is None:
        return compute()
    if key not in memo:
        memo[key] = compute()
    return memo[key]


def _completion(workspace: Workspace) -> dict:
    return _once("completion", lambda: rcm_execution.completion(workspace))


def _fieldwork_ran(workspace: Workspace) -> bool:
    """Whether any test in either register has a durable result.

    Whether a test has run is `rcm_execution`'s question, and the answer is the
    result artifact — a document test's item executions, a data test's
    `last_run` — never the status beside it. Paraphrasing it here as "any status
    outside draft/ready" put `blocked` on the executed side of the line, and
    three document tests the runner had refused to start for want of evidence
    reported a whole engagement's fieldwork as done: 47 tests specified, not one
    result, and no Run button anywhere on the ledger to start them.

    Both registers, read directly rather than through
    `rcm_execution.test_manifest`, which covers only the tests linked to a row of
    the matrix: an executed test that is not linked yet is still a result the
    engagement holds, and fieldwork having run is not a claim about coverage.
    The predicate is the manifest's own, so the row and the readiness sentence
    beside it cannot disagree about what "ran" means.
    """
    return any(
        rcm_execution.data_test_has_durable_result(item)
        for item in workspace.data_tests
    ) or any(
        rcm_execution.doc_test_has_durable_result(item)
        for item in _loaded_document_tests(workspace)
    )


def _doc_tests_ran(workspace: Workspace) -> bool | None:
    """Whether the document-test register holds a result.

    ``None`` where there is no document test to run, on the same reading
    `analysis.executed` gives an engagement with no tables: a stage with nothing
    of its kind to work on has nothing to hold and nothing to owe.

    Without this the row fell through to its `count`, which is
    `document_tests` — the size of the register `tests.specified` fills. The row
    labelled "Document test results" therefore went green, and stated the number
    of results it held, the moment the tests were *written*: 32 specifications
    drawn as 32 results on an engagement that had executed none of them.
    """
    tests = _loaded_document_tests(workspace)
    if not tests:
        return None
    return any(rcm_execution.doc_test_has_durable_result(item) for item in tests)


def _conclusions_set(workspace: Workspace) -> bool:
    """Whether the roll-up has concluded on the matrix.

    `rcm_execution.completion` owns what an unconcluded row is, and
    `_open_points` already reads it for the unread-conclusion debt. An empty
    matrix has nothing to conclude, which is not the same as the roll-up having
    run, so it is answered here rather than read as satisfied.
    """
    if not workspace.rcm:
        return False
    completion = _completion(workspace)
    return not (completion.get("rcm_without_conclusion") or [])


# Whether the engagement *holds* a work product, for the few stages whose
# artifact has no count to read. Everything else answers this with `_counts`.
#
# Deliberately not `Readiness.satisfied`, which answers a different question.
# Readiness is what the scheduler needs — whether a stage still has work to do —
# and on a register holding thirty findings with two observations still
# undrafted it reads "missing". It also cascades: `workflow_state` overwrites a
# capability's state with "blocked" when a dependency is unsatisfied, so a
# report of sixty thousand characters reads "blocked" because an earlier stage
# has residual work. The ledger asks what the engagement holds, and the honest
# answer to that is the artifact, not the schedule.
_HOLDS: dict[str, Any] = {
    # Either kind. A data-only engagement and a document-only one are both
    # real; requiring both would report an engagement as empty while it holds
    # everything it is going to.
    "sources.imported": lambda ws: bool(ws.documents or ws.table_names()),
    # ``None`` where there is nothing of that kind to analyse, which is the
    # answer the capabilities' own readiness gives: with no document in scope
    # `documents.analysis_generated` reads `satisfied`. Left to the count alone
    # both stages read as owed on an engagement carrying only the other kind of
    # source, and a documents-only engagement was offered a run over tables it
    # does not have. Not ``True`` either: a vacuous stage has filed nothing, and
    # saying it holds a work product puts a second entry in the totals of an
    # engagement that holds one.
    "analysis.executed":
        lambda ws: True if ws.analyses else (None if not ws.table_names() else False),
    "documents.analysis_generated":
        lambda ws: True if _document_analyses(ws) else (None if not ws.documents else False),
    "planning.apm_ready":
        lambda ws: bool(str((ws.planning or {}).get("apm_markdown") or "").strip()),
    "doc_tests.executed": _doc_tests_ran,
    "fieldwork.executed": _fieldwork_ran,
    "results.rolled_up": _conclusions_set,
    "report.working_draft": lambda ws: bool(_report_markdown(ws).strip()),
}

# What a stage's work product is called in a sentence about waiting for it.
# Only the wording lives here; which stage waits for which comes from the graph.
_NOUNS = {
    "sources.imported": "the sources",
    # Both analysis stages are prerequisites a reader can see and start — the
    # library and the per-document analyses are drawn on this ledger with their
    # own counts — so a stage waiting on one says which.
    "analysis.executed": "the analyses",
    "documents.analysis_generated": "the document analyses",
    "planning.apm_ready": "the memorandum",
    "planning.rcm_ready": "the matrix",
    "tests.specified": "the tests",
    "fieldwork.executed": "the test results",
    "results.rolled_up": "the conclusions",
    "findings.drafted": "the findings",
    "report.working_draft": "the report",
}


def _document_test_index(workspace: Workspace):
    """The document-test register, read once and kept in both its shapes.

    `doc_tests.list_tests` returns summaries, which is everything counting and
    linking need and is what the register's size means. It is *not* enough to
    ask whether a test has run: a summary carries a status and `state_counts`
    but no ``items``, and a cycle test's result is only ever its items, so
    `rcm_execution.doc_test_has_durable_result` reads one as never executed
    however much work is behind it. The loaded records answer that, and both
    come out of one index so the count and the presence test cannot end up
    describing different objects.

    Loading every test on top of listing them is free here: `_completion`
    already builds this index inside the same `doc_tests.request_cache_scope`,
    and `load_test` is cached within it.
    """
    def read():
        try:
            return rcm_execution.document_test_index(workspace)
        except Exception:
            return rcm_execution.DocumentTestIndex(tests=(), by_rcm_id={}, summaries=())

    return _once("document_test_index", read)


def _document_tests(workspace: Workspace) -> list[dict]:
    """The register as summaries — what `_counts` sizes and `_linked_test_count`
    walks. Never handed to a presence test; see `_document_test_index`."""
    return list(_document_test_index(workspace).summaries)


def _loaded_document_tests(workspace: Workspace) -> tuple[dict, ...]:
    """The register as loaded records — the only shape that can say what ran."""
    return _document_test_index(workspace).tests


def _document_analyses(workspace: Workspace) -> int:
    """How many documents hold a generated analysis.

    The same test the capability's own readiness runs, rather than a second
    reading of what "analysed" means: the row's count and the sentence beside
    it are drawn from one answer, so they cannot disagree on screen again.

    Cheap enough to ask per document — each is a small index read that already
    degrades to "absent" on its own when the file is missing — and memoized for
    the life of one `record()` call, which `_readiness` then repeats for free
    inside the same `_one_read`.
    """
    return _once("document_analyses", lambda: sum(
        1 for item in workspace.documents
        if has_generated_analysis(workspace, str(item.get("id") or ""))
    ))


def _plan_order() -> dict[str, int]:
    """Stage order as the engagement plan declares it."""
    try:
        outcomes = engagement.plan_outcomes(engagement.DEFAULT_TEMPLATE)
    except Exception:
        return {}
    return {str(item.get("capability") or ""): index for index, item in enumerate(outcomes)}


def _positions() -> dict[str, float]:
    """Where each stage sits on the ledger.

    The plan's order wherever the plan has one. A capability it does not
    contain — document tests are their own workflow — takes the place just
    after the stage declared before it in `_SPINE`, which reads as the work it
    belongs beside. Sorting those last instead put a register of sixteen
    executed tests below the stages that have not run.

    A stage that depends on nothing is the exception. The plan's order for it is
    arbitrary — any topological sort may put a root anywhere before its
    dependents — and the closure happens to reach `sources.imported` after the
    whole analysis branch, which would file the evidence base below the analyses
    read out of it. A root therefore takes its declared place instead. Stages
    with real dependencies keep the plan's answer, which is why document
    analyses still sit where the plan runs them rather than where they are
    declared.
    """
    plan = _plan_order()
    positions: dict[str, float] = {}
    previous = -1.0
    for capability in _SPINE:
        placed = plan.get(capability)
        if placed is not None and not _dependencies(capability):
            previous = previous + 0.5
        elif placed is not None:
            previous = float(placed)
        else:
            previous = previous + 0.5
        positions[capability] = previous
    return positions


def _registered() -> frozenset[str]:
    """Every capability the workflows still declare, across all four graphs.

    Membership here is what separates a stage the *record* has not learned yet
    from one the product no longer has. Both are absent from `_SPINE`, and they
    want opposite treatment: a new stage must not vanish because the record is
    behind, and a retired one must not linger because a milestone remembers it.
    """
    try:
        return frozenset(
            capability.id
            for registry in audit_capabilities.REGISTRY_BY_WORKFLOW.values()
            for capability in registry.all()
        )
    except Exception:
        # Unable to tell the two apart, so keep everything: a stale row is a
        # smaller failure than losing a stage the record cannot name yet.
        return frozenset()


def _live_bodies(workspace: Workspace) -> dict[str, dict]:
    """Each stage's body, recomputed against the workspace as it stands.

    A milestone's body is a photograph. `planning.rcm_ready` filed "25 rows
    covering 4 processes" over a distribution of 0 critical, 22 high and 3
    medium; the matrix now holds 22 rows rated 5 high, 12 medium and 5 low.
    The count in the row's own artifact block was already read from the
    workspace, so the row stated 22 and then described 25 — the number and the
    sentence beside it disagreed on screen.

    This is the same projection the milestone was built from, re-run now, so
    nothing about how a matrix or a test register is read is restated here.

    Only what the projection derives from workspace state is taken: the
    sentence, the severity tally, and the rows it reads out. Metrics are left
    alone, because "Created 25, Updated 0, Preserved 0" describes one run's
    delta, and with no run to describe it would report zeros as though nothing
    had ever been made.
    """

    def read() -> dict[str, dict]:
        try:
            from .agent.audit_execution import AuditWorkflowExecution

            registry = audit_capabilities.REGISTRY_BY_WORKFLOW[audit_workflow.WORKFLOW_ID]
            execution = AuditWorkflowExecution(workspace, {}, None)
        except Exception:
            return {}
        bodies: dict[str, dict] = {}
        for capability_id in _SPINE:
            try:
                capability = registry.get(capability_id)
                body = execution.milestone_projection(workspace, {}, capability, {})
            except Exception:
                # A projection that cannot answer leaves the stage on its
                # milestone rather than blanking a row that has something true
                # to say, even if it is old.
                continue
            if isinstance(body, dict):
                bodies[capability_id] = body
        return bodies

    return _once("live_bodies", read)


def _readiness(workspace: Workspace) -> dict[str, dict]:
    """What every capability says about its own output.

    Four registries, because the record draws stages from more than one
    workflow: document tests are their own workflow rather than a step of the
    audit plan, so no single closure contains everything drawn here. Merged the
    way `assistant_chats` already merges them.

    Affordable only inside `_one_read`, which holds the document-test cache
    these readiness functions repeatedly fall through.

    Only the spine and what it depends on is asked. The ledger draws twelve
    rows; the four registries declare forty-six capabilities, and the full
    sweep ran every one of them — the disposition of every document test, the
    stamping of every schema — to fill rows that never read the answer. The
    closure keeps each drawn row's cascaded state exactly as the full sweep
    would have given it, because a state depends on nothing outside it.

    A registry that cannot answer is skipped rather than allowed to empty the
    row: state read from the workspace is the half of the record that has to
    survive when something else is missing.
    """

    def read() -> dict[str, dict]:
        state: dict[str, dict] = {}
        drawn = tuple(_SPINE)
        for project in (
            audit_capabilities.workflow_state,
            audit_capabilities.analysis_workflow_state,
            audit_capabilities.documents_workflow_state,
            audit_capabilities.doc_tests_workflow_state,
        ):
            try:
                state.update(project(workspace, only=drawn))
            except Exception:
                continue
        return state

    return _once("readiness", read)


def _dependencies(capability: str) -> tuple[str, ...]:
    """A stage's direct dependencies, from the graph that owns them."""
    try:
        return tuple(audit_workflow.dependencies(capability))
    except Exception:
        return ()


def _blocked_by(workspace: Workspace, capability: str, counts: dict[str, int]) -> str:
    """Why a stage cannot start, or '' when nothing holds it.

    Read from the authoritative graph rather than restated here, the way the
    capability modules already read it. Restating drifted: the record had
    `findings.drafted` waiting on `tests.specified` while the graph had it
    waiting on `results.rolled_up`, so an engagement whose tests were specified
    and never run was invited to draft findings from exceptions that did not
    exist — and told nothing was blocking it.

    Only a dependency the record can test the presence of is reported. The rest
    are machine steps whose absence is not observable, and naming one would tell
    a reader to wait for something they cannot see or start.

    The walk goes *through* those machine steps rather than stopping at them,
    which is the difference between naming a blocker and losing it. The APM
    depends on `planning.context_ready`, which is not a row on this ledger; one
    hop found nothing nameable and the record told an engagement holding
    eighty-four unanalysed documents that nothing was blocking the memorandum.
    Two hops reach `documents.analysis_generated`, which is drawn, is counted,
    and is exactly what the reader has to run first.
    """
    waiting: list[str] = []
    seen: set[str] = set()

    def walk(target: str) -> None:
        for dependency in _dependencies(target):
            if dependency in seen:
                continue
            seen.add(dependency)
            if dependency not in _NOUNS:
                # Unnameable. A stage the ledger does not draw at all is a
                # machine step standing between two things it does draw, so the
                # walk continues past it; one the ledger draws without a noun
                # has been left deliberately unnameable and ends the branch.
                if dependency not in _SPINE:
                    walk(dependency)
                continue
            # A presence test that cannot answer must not invent a blocker
            # either, so only a dependency known to be absent is named. A
            # dependency that is held ends the branch: what it was itself
            # waiting for stopped mattering when it was filed.
            if _holds(workspace, dependency, counts) is not False:
                continue
            noun = _NOUNS[dependency]
            if noun not in waiting:
                waiting.append(noun)

    walk(capability)
    if not waiting:
        return ""
    if len(waiting) == 1:
        return f"Waits for {waiting[0]}."
    return f"Waits for {', '.join(waiting[:-1])} and {waiting[-1]}."


def _holds(workspace: Workspace, capability: str, counts: dict[str, int]) -> bool | None:
    """Whether the engagement holds this stage's work product.

    A count answers it wherever the work product is a register with a size.
    `_HOLDS` answers the rest and takes precedence: the conclusions row is
    sized by the matrix it concludes on, so twenty-two rows would otherwise
    read as twenty-two conclusions the moment the matrix existed.

    ``None`` is a third answer, and a load-bearing one. Two things give it: a
    presence test that raised does not know, and "does not know" must never
    collapse into "absent"; and a stage with nothing of its kind to work on has
    nothing to hold *and* nothing to owe. A row answering ``None`` is drawn
    without an invitation to run it and is counted among neither the work
    products held nor the work outstanding — which is right for both, because
    inviting an auditor to redo work that may already exist and inviting one to
    analyse documents they never imported are the same kind of mistake.
    """
    test = _HOLDS.get(capability)
    if test is not None:
        try:
            answer = test(workspace)
        except Exception:
            return None
        return None if answer is None else bool(answer)
    key = (_SPINE.get(capability) or {}).get("count")
    return bool(counts.get(key)) if key else False


def _stages(
    workspace: Workspace,
    counts: dict[str, int],
    history: dict[str, dict],
    points: dict[str, list[dict]],
) -> list[dict]:
    """One row per work product, in plan order — the whole ledger.

    Every stage the record can draw appears every time, whether or not a run
    ever filed it. That is the difference between this and the two half-ledgers
    it replaces. A row used to exist because a milestone said so, which is why
    losing a run folder emptied a record whose workspace held eleven work
    products, why a stage that produced its artifact without narrating appeared
    in neither half, and why a stage that was *running* appeared in neither and
    had to be drawn by the caller from a published vocabulary.
    """
    order = _positions()
    readiness = _readiness(workspace)
    bodies = _live_bodies(workspace)
    rows = []
    for capability, spec in _SPINE.items():
        holds = _holds(workspace, capability, counts)
        # Only a stage known to be absent is owed. `None` — a presence test that
        # could not answer — is neither held nor owed.
        owed = holds is False
        blocked = _blocked_by(workspace, capability, counts) if owed else ""
        headline = str(spec.get("headline") or "")
        count_key = spec.get("count")
        state = readiness.get(capability) or {}
        unit = str(spec.get("unit") or "")
        # Live where the projection can answer, the milestone's own account
        # otherwise — a stage narrated by another workflow has no audit
        # projection, and its filed body is still true about what it filed.
        # A stage that holds nothing describes nothing: the live body of an
        # empty matrix is a sentence about zero rows, and the row already says
        # it has not run.
        past = (history.get(capability) or {}) if holds is True else {}
        body = (bodies.get(capability) or {}) if spec.get("live_body") and holds is True else {}
        rows.append({
            "id": f"stage:{capability}",
            "capability": capability,
            # Its place on the ledger, which is the plan's order where the plan
            # has one — see `_positions`.
            "order": order.get(capability),
            "held": holds is True,
            # Owed only where the record knows how to ask for it. Verification
            # commits nothing, so its absence is not something a reader could
            # act on, and it is never drawn as work outstanding.
            "runnable": bool(headline) and owed and not blocked,
            "headline": headline,
            "blocked_reason": blocked,
            # The row key is the outcome to request wherever the stage is
            # terminal for its own workflow. Where it is not, the spec declares
            # what the button asks for — see `analysis.executed` and
            # `documents.analysis_generated`. `alternates` is a narrower set the
            # auditor may pick deliberately; the primary click stays complete.
            "start": (
                {
                    "prompt": spec["prompt"],
                    "outcomes": list(spec.get("outcomes") or (capability,)),
                    "alternates": [
                        {
                            "label": str(alternate["label"]),
                            "prompt": str(alternate["prompt"]),
                            "outcomes": list(alternate["outcomes"]),
                            "note": str(alternate.get("note") or ""),
                        }
                        for alternate in spec.get("alternates") or ()
                    ],
                }
                if headline and owed and spec.get("prompt") else None
            ),
            # How this stage is begun. Everything the agent produces is "run";
            # "import" is the auditor's own act, and the shell owns the dialog
            # that performs it.
            "action": str(spec.get("action") or ("run" if headline else "")),
            # Doors beside the artifact card, for a stage that opens more than
            # one thing or offers a tool alongside what it filed.
            "links": [
                {
                    "label": str(link.get("label") or ""),
                    "destination": str(link.get("destination") or ""),
                    "count": counts.get(link["count"]) if link.get("count") else None,
                    "kind": str(link.get("kind") or "artifact"),
                }
                for link in spec.get("links") or ()
            ],
            "filed": {
                "label": spec.get("label") or capability,
                "destination": spec.get("destination") or "",
                "unit": unit,
                # Irregular plurals are declared beside the unit rather than
                # left to the caller, which produced "28 analysiss".
                "unit_plural": spec.get("unit_plural") or (f"{unit}s" if unit else ""),
                "count": counts.get(count_key) if count_key else None,
            },
            # The graph's own answer, carried rather than collapsed into
            # `held`: it says what is *left* where `held` says what exists, and
            # on a register that is thirty drafted and two short those are two
            # different sentences, both true.
            "readiness": {
                "state": str(state.get("state") or ""),
                "reasons": list(state.get("reasons") or []),
                "details": {
                    key: value for key, value in state.items()
                    if key not in ("state", "reasons", "blocking_on")
                },
            },
            # What this stage amounts to now. The severity tally in particular
            # is read at a glance and was the most misleading thing on the row:
            # a matrix rated "22 high" when it holds five.
            "summary": str(body.get("summary") or past.get("summary") or ""),
            "stats": list(body.get("stats") or past.get("stats") or []),
            "highlights": list(body.get("highlights") or past.get("highlights") or []),
            "live_body": bool(body),
            "open_points": points.get(capability, []),
            "history": history.get(capability),
        })
    rows.sort(key=lambda row: row["order"])

    # A stage the record has never heard of still filed something, and a reader
    # is better served by its own briefing than by its disappearance. It carries
    # no artifact block because nothing here knows what it produced or where to
    # open it, and it sits after the plan rather than inside it.
    #
    # A stage the *workflows* no longer declare is the other case and gets the
    # opposite answer. Dashboard curation was retired from the audit graph; a
    # milestone on an engagement that ran it while it existed must not keep
    # drawing a row for a step the product does not have any more.
    registered = _registered()
    for capability, filed in history.items():
        if capability in _SPINE or capability not in registered:
            continue
        rows.append({
            "id": f"stage:{capability}",
            "capability": capability,
            "order": None,
            "held": True,
            "runnable": False,
            "headline": "",
            "blocked_reason": "",
            "start": None,
            # Present and empty, not absent. Every row the record draws carries
            # the same keys, and a reader of one is entitled to the shape of
            # the others: the frontend reads `stage.links.length` unguarded,
            # because the contract says it is always a list. Omitting it here
            # emptied the whole Record view the first time an engagement filed
            # a stage outside the spine — working papers, which is registered
            # and deliberately has no spine row.
            "action": "",
            "links": [],
            "filed": None,
            "readiness": {"state": "", "reasons": [], "details": {}},
            "summary": str(filed.get("summary") or ""),
            "stats": list(filed.get("stats") or []),
            "highlights": list(filed.get("highlights") or []),
            "live_body": False,
            "open_points": points.get(capability, []),
            "history": filed,
        })
    return rows


# --------------------------------------------------------------------------- #
# What a filed stage left open
# --------------------------------------------------------------------------- #
# Rank is deliberate, and it puts review ahead of unstarted work. Running the
# next stage is something the agent does by itself in auto mode; reading what it
# decided is the one thing only a person can do, so that is what the record asks
# for first.
# The two findings debts share one rank because they share one slot: only
# ever one of them is raised, and which one is decided in `_open_points`.
_OPEN_RANK = {
    "unread_conclusions": 10, "unconfirmed_findings": 20,
    "findings_followup": 20, "draft_rcm": 30,
}


def _open_points(workspace: Workspace) -> list[dict]:
    """Debts left behind by stages that completed."""
    points: list[dict] = []

    try:
        completion = _completion(workspace)
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

    # The findings row carries one debt at a time, and confirmation is the one
    # that comes first. Only a confirmed finding reaches the report at all --
    # `report.build_context` carries `auditor_confirmed` findings and no others
    # -- so an unconfirmed register produces a report that states no findings
    # were identified over one holding twenty-four. Asking for root causes while
    # that is true names the smaller debt and hides the larger: the causes would
    # be written into findings the report still would not carry.
    unconfirmed = [
        item for item in workspace.findings if not item.get("auditor_confirmed")
    ]
    owed = [
        item for item in workspace.findings
        if item.get("cause_pending")
        or not str(item.get("management_response") or "").strip()
    ]
    if unconfirmed:
        points.append({
            "key": "unconfirmed_findings",
            "capability": "findings.drafted",
            "message": (
                f"{len(unconfirmed)} of {len(workspace.findings)} findings are "
                "not confirmed for reporting"
                # Only true while nothing has been confirmed. On a part-confirmed
                # register the report carries the rest, so the sentence says what
                # is being left out rather than claiming the report is empty.
                + (" — the report will carry none of them."
                   if len(unconfirmed) == len(workspace.findings)
                   else " and will be left out of the report.")
            ),
            "action": "Confirm findings",
            "destination": "findings",
        })
    elif owed:
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

    # Unsigned rather than literally ``draft``, which is the same predicate the
    # planning status bar filters on. The two must agree: a row this counts as
    # outstanding is a row that screen offers to sign.
    draft = [row for row in workspace.rcm if str(row.get("review_status") or "") != "reviewed"]
    if draft:
        points.append({
            "key": "draft_rcm",
            "capability": "planning.rcm_ready",
            "message": (
                f"{len(draft)} of {len(workspace.rcm)} rows are still marked draft."
                # Only true while nothing has been signed. Stated unconditionally,
                # it contradicted its own first sentence on a part-reviewed matrix.
                + (" None has been reviewed." if len(draft) == len(workspace.rcm) else "")
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
    with _one_read():
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

        history = {
            capability: _history(capability, rows)
            for capability, rows in by_capability.items()
        }
        points = _open_points(workspace)
        by_capability_point: dict[str, list[dict]] = {}
        for point in points:
            by_capability_point.setdefault(point["capability"], []).append(point)

        stages = _stages(workspace, counts, history, by_capability_point)
        # Every debt now has a row to sit on: the stage that owes it is drawn
        # whether or not it ever filed. `orphaned_points` existed because a debt
        # could outlive the only row that would have carried it.
        held = [stage for stage in stages if stage["held"]]

        # Review outranks unstarted work — see `_OPEN_RANK`.
        first_runnable = next((row for row in stages if row["runnable"]), None)
        upcoming = points[0] if points else None
        next_step = (
            {"kind": "open_point", **upcoming} if upcoming
            else {"kind": "stage", **first_runnable} if first_runnable
            else None
        )

        settled = [stage["history"] for stage in stages if stage["history"]]
        measured = [item["elapsed_ms"] for item in settled if item["elapsed_ms"] is not None]
        timeline = sorted(str(item["at"] or "") for item in settled)
        return {
            "stages": stages,
            "open_points": points,
            "next": next_step,
            "counts": counts,
            "totals": {
                # What the engagement holds, which is no longer the same as what
                # a run was seen to file: a stage whose runs are gone still holds
                # its work product, and says so.
                "work_products": len(held),
                "runs": len(runs),
                # A run that committed nothing filed nothing; saying so is more
                # honest than a record that silently drops a third of the history.
                "runs_that_filed": len(contributing),
                "attempts": sum(len(item["attempts"]) for item in settled),
                "elapsed_ms": sum(measured) if measured else None,
                "first_at": min((str(item["first_at"] or "") for item in settled), default=None) or None,
                "last_at": timeline[-1] if timeline else None,
            },
        }
