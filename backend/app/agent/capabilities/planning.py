"""Planning capability group of the audit workflow.

Owns the planning outcomes of the authoritative audit graph:
``planning.context_ready``, ``planning.apm_ready``, ``planning.cycle_ready``
and ``planning.rcm_ready``.
Drafting the tests an RCM row needs belongs to the tests capability group.

Each capability is declared here: its readiness, its semantic unit expansion,
and the registry keys for its declared context. The dependency edges come from
the authoritative graph in :mod:`agent.workflows.audit`; this module never
restates them.

Readiness in this group is existence, structural usability, *and* currency —
alone among the capability groups, because alone among them these artifacts
carry a ``workflow_parents`` stamp written by their executors inside the guarded
commit. A memorandum that has been rewritten leaves the shape and the matrix
read out of it describing a process the engagement no longer says it audits, and
that is a thing a stamp can prove rather than a thing to guess at. What still
never makes them stale is a *source*: a table or a document arriving is the
auditor's act, nothing in the graph produces it, and no planning artifact
declares it as a parent.
"""

from __future__ import annotations

from ... import planning_delta
from ...text import counted, verb
from ...workspaces import Workspace, planning_apm_sha1
from ..workflow import Capability, Readiness, UnitSpec
from ..workflows import audit as audit_workflow
from ._shared import Currency
from ._shared import currency as _currency
from ._shared import rows as _rows
from ._shared import single_unit as _single
from ._shared import with_currency as _with_currency

CAPABILITY_IDS: tuple[str, ...] = (
    "planning.context_ready",
    "planning.apm_ready",
    "planning.cycle_ready",
    "planning.rcm_ready",
    "planning.change_assessed",
)


# --------------------------------------------------------------------------- #
# planning.context_ready (P7A)
# --------------------------------------------------------------------------- #
def _context_ready(workspace: Workspace, _scope: dict) -> Readiness:
    context = workspace.planning.get("context") or {}
    if any(
        str(value or "").strip()
        for field, value in context.items()
        if field != "interview_answers"
    ) or context.get("interview_answers"):
        return Readiness("satisfied")
    # Whether anything has been imported is `sources.imported`'s question now,
    # and this capability depends on it, so an empty workspace reaches here
    # already blocked by the cascade rather than by a second copy of the test.
    return Readiness("missing", ("planning context has not been established",))


def _context_units(_workspace: Workspace, _scope: dict) -> list[UnitSpec]:
    return [
        UnitSpec(
            "planning_context",
            "planning_context",
            "Assemble planning context",
            (),
            (),
        )
    ]


def _planning_context_ready() -> Capability:
    return Capability(
        "planning.context_ready",
        "planning_context",
        "Planning context",
        "planning_context",
        audit_workflow.dependencies("planning.context_ready"),
        _context_ready,
        _context_units,
        context="planning.context",
        invalidate_on=("sources",),
    
        produces=("planning",),
        accepts_refs=(),
    )


# --------------------------------------------------------------------------- #
# planning.apm_ready (P7B)
# --------------------------------------------------------------------------- #
def _apm_ready(workspace: Workspace, _scope: dict) -> Readiness:
    markdown = str(workspace.planning.get("apm_markdown") or "").strip()
    if not markdown:
        return Readiness("missing", ("APM content is empty",))
    if not any(line.lstrip().startswith("#") for line in markdown.splitlines()):
        return Readiness("review_required", ("APM has no structured headings",))
    # Existence, structural usability — and currency against the one parent
    # the memorandum declares. Not against its *sources*: a table or a document
    # arriving does not make a memorandum wrong, and the whole point of the
    # invalidation model is that only a declared parent moving does.
    state = _currency(workspace, workspace.planning.get("workflow_parents"))
    if state.stale:
        return Readiness(
            "stale",
            ("the memorandum was drafted against an earlier planning context",),
            details={"artifact_count": 1, **state.details},
        )
    return _with_currency(Readiness("satisfied", details={"artifact_count": 1}), state)


def _planning_apm_ready() -> Capability:
    return Capability(
        "planning.apm_ready",
        "apm",
        "Audit planning memorandum",
        "apm",
        audit_workflow.dependencies("planning.apm_ready"),
        _apm_ready,
        _single("apm", "Draft audit planning memorandum", "planning:context"),
        context="planning.apm",
        invalidate_on=("planning:context",),
    
        produces=("planning",),
        accepts_refs=(),
    )


# --------------------------------------------------------------------------- #
# planning.cycle_ready
# --------------------------------------------------------------------------- #
def _cycle_ready(workspace: Workspace, _scope: dict) -> Readiness:
    """Whether a cycle shape exists for the memorandum currently in the file.

    Currency is assessed here, unlike every other planning capability, and for
    a reason that is particular to this artifact: the shape is a reading *of*
    the memorandum's process flow, so a memorandum that has been rewritten has
    left the shape describing a process the engagement no longer says it audits
    — and the matrix downstream takes its ``process`` vocabulary from it.

    An auditor's edit is the confirmation and keeps the hash it was drafted
    against, so edits survive until the memorandum itself moves. Nothing waits
    on a review that may never come.

    It reports ``stale`` rather than ``missing``, which it used to: a shape
    that exists and describes the wrong process is not an absent shape, and the
    scheduler now has a state that says exactly that.
    """
    cycle = workspace.planning.get("cycle") or {}
    steps = cycle.get("steps") or []
    if not steps:
        return Readiness("missing", ("no cycle has been designed",))
    reason = "the cycle was designed against an earlier memorandum"
    state = _currency(workspace, cycle.get("workflow_parents"))
    if state.stale:
        return Readiness(
            "stale",
            (reason,),
            details={"artifact_count": len(steps), **state.details},
        )
    # A shape committed before parents were stamped still has the memorandum
    # text hash it was drafted against, so it can answer the same question —
    # narrowly, and only for itself.
    if state.state == "unstamped" and str(
        cycle.get("apm_sha1") or ""
    ) != planning_apm_sha1(workspace):
        return Readiness(
            "stale",
            (reason,),
            details={
                "artifact_count": len(steps),
                "currency": "stale",
                "moved": ["planning:apm"],
            },
        )
    return _with_currency(
        Readiness("satisfied", details={"artifact_count": len(steps)}), state
    )


def _planning_cycle_ready() -> Capability:
    return Capability(
        "planning.cycle_ready",
        "cycle",
        "Cycle design",
        "cycle",
        audit_workflow.dependencies("planning.cycle_ready"),
        _cycle_ready,
        _single("cycle", "Design the cycle", "planning:apm"),
        context="planning.cycle",
        invalidate_on=("planning:apm",),
    
        produces=("planning",),
        accepts_refs=(),
    )


# --------------------------------------------------------------------------- #
# planning.rcm_ready (P7C)
# --------------------------------------------------------------------------- #
def _rcm_ready(workspace: Workspace, scope: dict) -> Readiness:
    rows = _rows(workspace, scope)
    if not rows:
        return Readiness(
            "missing",
            ("no RCM rows exist for the requested scope",),
            details={"artifact_count": 0},
        )
    invalid = [
        row["id"]
        for row in rows
        if not str(row.get("risk") or "").strip()
        or not str(row.get("control") or "").strip()
    ]
    if invalid:
        return Readiness(
            "review_required",
            (f"{counted(len(invalid), 'RCM row')} {verb(len(invalid), 'lacks', 'lack')} a risk or control",),
            details={"artifact_count": len(rows)},
        )
    # Currency is per row, because the matrix is committed per row and the
    # executor reconciles per row. Readiness is whole-matrix — one stale row
    # schedules the stage — and the stage then redrafts the matrix, preserving
    # every auditor-owned row exactly as it does today.
    states = [_currency(workspace, row.get("workflow_parents")) for row in rows]
    outdated = [row["id"] for row, state in zip(rows, states) if state.stale]
    if outdated:
        return Readiness(
            "stale",
            (
                f"{counted(len(outdated), 'RCM row')} "
                f"{verb(len(outdated), 'was', 'were')} drafted against earlier "
                "planning",
            ),
            details={
                "artifact_count": len(rows),
                "currency": "stale",
                "moved": list(
                    dict.fromkeys(ref for state in states for ref in state.moved)
                ),
                "stale_rows": outdated,
            },
        )
    return _with_currency(
        Readiness("satisfied", details={"artifact_count": len(rows)}),
        Currency("current")
        if any(state.state == "current" for state in states)
        else Currency("unstamped"),
    )


def _planning_rcm_ready() -> Capability:
    return Capability(
        "planning.rcm_ready",
        "rcm",
        "Risk and control matrix",
        "rcm",
        audit_workflow.dependencies("planning.rcm_ready"),
        _rcm_ready,
        _single(
            "rcm", "Draft risk and control matrix", "planning:apm", "planning:cycle"
        ),
        context="planning.rcm",
        invalidate_on=("planning:apm", "planning:cycle"),
    
        produces=("rcm",),
        accepts_refs=(),
    )


# --------------------------------------------------------------------------- #
# planning.change_assessed (step 9)
# --------------------------------------------------------------------------- #
def _assessed_documents(scope: dict) -> tuple[str, ...]:
    """The documents this request asked about, in request order."""

    refs = [str(value) for value in scope.get("target_refs") or []]
    named = [ref.split(":", 1)[1] for ref in refs if ref.startswith("document:")]
    return tuple(dict.fromkeys(value for value in named if value))


def _change_assessed(workspace: Workspace, scope: dict) -> Readiness:
    """Whether this exact question has already been answered.

    Not "is the memorandum current" — the framework does not ask that, and this
    capability is not a watch. The question is narrower and answerable: has an
    assessment been made against *these* documents, *this* memorandum and *this*
    matrix. Change any of the three and the stored answer is an answer to a
    different question, so readiness goes missing and the outcome re-runs.
    """

    documents = _assessed_documents(scope)
    if not documents:
        return Readiness(
            "blocked",
            ("name the documents to assess",),
            details={"assessed": 0},
        )
    # What the assessment compares against is now a pair of dependency edges
    # rather than a second existence check written out here. The edges could
    # not be declared while a scheduled dependency was itself a reason to
    # rewrite settled work — naming the memorandum would have rewritten it —
    # and now that only a declared parent moving does that, the graph says it
    # once and the projection reports it blocked for free.
    basis = planning_delta.basis_sha1(workspace, list(documents))
    assessment = planning_delta.load(workspace, basis)
    if assessment is None:
        return Readiness(
            "missing",
            (f"{counted(len(documents), 'document')} not yet assessed",),
            details={"documents": list(documents)},
        )
    return Readiness(
        "satisfied",
        details={"impact": assessment.get("impact"), "basis_sha1": basis},
    )


def _change_assessment_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    documents = _assessed_documents(scope)
    if not documents:
        return []
    basis = planning_delta.basis_sha1(workspace, list(documents))
    if planning_delta.load(workspace, basis) is not None and not _forced(scope):
        return []
    return [
        UnitSpec(
            "change_assessment",
            "change_assessment",
            f"Assess what {counted(len(documents), 'document')} changes",
            tuple(f"document:{value}" for value in documents),
            {"document_ids": list(documents), "basis_sha1": basis},
        )
    ]


def _forced(scope: dict) -> bool:
    return str(scope.get("generation_mode") or "") == "force"


def _planning_change_assessed() -> Capability:
    return Capability(
        "planning.change_assessed",
        "change_assessment",
        "Change assessment",
        "delta_review",
        audit_workflow.dependencies("planning.change_assessed"),
        _change_assessed,
        _change_assessment_units,
        context="planning.delta",
        # The assessment reads the memorandum and the matrix, and is invalid
        # the moment either moves — which the basis already encodes, but a
        # reader of the declaration should not have to derive that.
        invalidate_on=("planning:apm", "rcm"),
        produces=(),
        accepts_refs=("document",),
    )


# Locally-owned declaration builders keyed by capability ID.
_BUILDERS = {
    "planning.context_ready": _planning_context_ready,
    "planning.apm_ready": _planning_apm_ready,
    "planning.cycle_ready": _planning_cycle_ready,
    "planning.rcm_ready": _planning_rcm_ready,
    "planning.change_assessed": _planning_change_assessed,
}


def capabilities() -> tuple[Capability, ...]:
    """Return this group's capability declarations in authoritative order."""

    return tuple(_BUILDERS[capability_id]() for capability_id in CAPABILITY_IDS)
