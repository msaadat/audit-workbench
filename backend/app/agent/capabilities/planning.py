"""Planning capability group of the audit workflow.

Owns the planning outcomes of the authoritative audit graph:
``planning.context_ready``, ``planning.apm_ready``, ``planning.cycle_ready``
and ``planning.rcm_ready``.
Drafting the tests an RCM row needs belongs to the tests capability group.

Each capability is declared here: its readiness (existence and structural
usability only), its semantic unit expansion, and the registry keys for its
declared context. The dependency edges come from the authoritative graph in
:mod:`agent.workflows.audit`; this module never restates them.
"""

from __future__ import annotations

from ...text import counted, verb
from ...workspaces import Workspace, planning_apm_sha1
from ..workflow import Capability, Readiness, UnitSpec
from ..workflows import audit as audit_workflow
from ._shared import rows as _rows
from ._shared import single_unit as _single

CAPABILITY_IDS: tuple[str, ...] = (
    "planning.context_ready",
    "planning.apm_ready",
    "planning.cycle_ready",
    "planning.rcm_ready",
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
    # Existence and structural usability only. Whether the APM is substantively
    # current with respect to changed planning sources is not assessed by the
    # framework; the auditor decides when to force a regeneration.
    return Readiness("satisfied", details={"artifact_count": 1})


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
    """
    cycle = workspace.planning.get("cycle") or {}
    if not cycle.get("steps"):
        return Readiness("missing", ("no cycle has been designed",))
    if str(cycle.get("apm_sha1") or "") != planning_apm_sha1(workspace):
        return Readiness(
            "missing",
            ("the cycle was designed against a different memorandum",),
            details={"artifact_count": 1},
        )
    return Readiness(
        "satisfied",
        details={"artifact_count": len(cycle.get("steps") or [])},
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
    # Structurally usable RCM rows exist; currency relative to the APM is not
    # assessed. Parent hashes remain on the rows for executor CAS/provenance.
    return Readiness("satisfied", details={"artifact_count": len(rows)})


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
    )


# Locally-owned declaration builders keyed by capability ID.
_BUILDERS = {
    "planning.context_ready": _planning_context_ready,
    "planning.apm_ready": _planning_apm_ready,
    "planning.cycle_ready": _planning_cycle_ready,
    "planning.rcm_ready": _planning_rcm_ready,
}


def capabilities() -> tuple[Capability, ...]:
    """Return this group's capability declarations in authoritative order."""

    return tuple(_BUILDERS[capability_id]() for capability_id in CAPABILITY_IDS)
