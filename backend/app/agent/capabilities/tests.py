"""Test capability group of the audit workflow.

Owns ``tests.specified`` — the single pass that generates the complete,
executable tests one RCM row needs and decides each test's source in one
model turn, per docs/test-capability-merge-plan.md.

A test is one record with one source. Generation writes the audit plan and
the executable part — Polars steps for a Data Test, items and checks for a
Document Test — onto the same record in one commit. Nothing in between is
durable, and the RCM row holds only ``test_refs``.

The capability is declared here: its readiness (existence and structural
usability only), its semantic unit expansion, and the registry keys for its
declared context. The dependency edges come from the authoritative graph in
:mod:`agent.workflows.audit`; this module never restates them.
"""

from __future__ import annotations

from ... import (
    analysis_promotion,
    cycle_measurement,
    cycle_rulesets,
    doc_tests,
    rcm_execution,
)
from ...text import counted, verb
from ...workspaces import Workspace
from ..workflow import Capability, Readiness, UnitSpec, semantic_unit_id
from ..workflows import audit as audit_workflow
from ._shared import named_test_ids_for_row as _named_test_ids_for_row
from ._shared import rows as _rows
from ._shared import target_rcm_ids as _target_rcm_ids

CAPABILITY_IDS: tuple[str, ...] = (
    "tests.cycle_ruleset_approved",
    "tests.specified",
    "tests.promoted_from_analysis",
)


def _scoped_manifest(workspace: Workspace, scope: dict) -> list[dict]:
    selected = set(_target_rcm_ids(workspace, scope))
    return [
        item
        for item in rcm_execution.test_manifest(workspace)
        if item["rcm_id"] in selected
    ]


def _testable(workspace: Workspace) -> bool:
    """Whether the workspace holds anything a test could actually be run against.

    A test is executable work, not a description, so with no imported table and
    no document there is nothing to generate: the row would get a plan that
    could never be executed.
    """
    return bool(workspace.tables or workspace.documents)


def _by_row(manifest: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for item in manifest:
        grouped.setdefault(item["rcm_id"], []).append(item)
    return grouped


def _unvouched_types(workspace: Workspace) -> list[str]:
    """Document types whose records were extracted and never vouched against.

    The bypass this reports is silent by construction. Cycle vouching is
    reachable only through a ``transaction_cycle`` control attribute; when the
    matrix classifies every attribute some other way, no cycle test is
    requested, the linker and the evaluator are never called, and the run
    reports success. An engagement whose documents were read against induced
    schemas and then never tie-matched has degraded from the strongest evidence
    path available to it, and must say so.

    Deliberately keyed on the extracted records rather than on the matrix: it
    is the matrix's classification that is in question, so it cannot also be
    the thing that decides whether the question gets asked.
    """
    if not workspace.documents:
        return []
    if any(
        doc_tests.is_cycle_test(test) for test in doc_tests.list_tests(workspace)
    ):
        return []
    try:
        records = cycle_measurement.structured_records(workspace)
    except Exception:
        # Readiness reports; it never fails the run on evidence it cannot read.
        return []
    return sorted({
        str(row.get("document_type") or "")
        for row in records
        if row.get("document_type")
    })


def _unvouched_cause(workspace: Workspace) -> tuple[str, str, str]:
    """Why nothing vouches records that were extracted to be vouched.

    ``_unvouched_types`` deliberately detects the gap from the records, so that
    the matrix cannot both be the thing in question and the thing that decides
    whether the question is asked. That keeps the detection honest and leaves
    the *explanation* unasked — and an explanation nobody checked is worse than
    none, because it sends the reader to the wrong repair. This asks.

    The causes are different repairs in different places: write a
    transaction-cycle attribute, run a proposal, approve one, or build the test.
    The approval case is the one that reads like success from every other angle
    — an agent proposed sound-looking rules, every stage reported completion,
    and the rules sit unapplied because approving them is not an agent's to do.

    Returns ``(cause, sentence, ruleset_id)``; ``ruleset_id`` is empty unless a
    specific proposal is what the reader has to go and look at.
    """

    if not _cycle_attributes(workspace):
        return (
            "no_cycle_attribute",
            "the matrix classified no control attribute as transaction_cycle",
            "",
        )
    rulesets = cycle_rulesets.list_rulesets(workspace)
    if not rulesets:
        return (
            "no_ruleset",
            "the matrix asks for transaction-cycle evidence and the cycle "
            "design proposed no rules linking the documents",
            "",
        )
    approved = cycle_rulesets.effective(workspace)
    if approved is None:
        pending = [
            record for record in rulesets
            if str(record.get("status")) == "proposed"
        ]
        if pending:
            latest = pending[-1]
            return (
                "ruleset_unapproved",
                f"{counted(len(pending), 'cycle ruleset')} "
                f"{verb(len(pending), 'is', 'are')} proposed and unapproved, so "
                "no cycle test can be built; review the rules and their "
                "measured fan-out, then approve",
                str(latest.get("ruleset_id") or ""),
            )
        return (
            "ruleset_rejected",
            "every proposed cycle ruleset has been rejected, so no cycle test "
            "can be built",
            "",
        )
    return (
        "no_cycle_test",
        "a cycle ruleset is approved and no cycle test was built against it",
        str(approved.get("ruleset_id") or ""),
    )


# --------------------------------------------------------------------------- #
# tests.specified
# --------------------------------------------------------------------------- #
def _awaits_cycle_test(workspace: Workspace, row: dict, tests: list[dict]) -> bool:
    """Whether this row's evidence strategy became available after its test.

    A ``transaction_cycle`` attribute generated a fallback while no ruleset was
    approved — correctly, and the run said so at the time. Approving one is a
    change in what the row *can* be answered with, and a row still holding the
    fallback is not covered in the sense this capability means: its requirement
    is answered by prose where linked evidence now exists.

    Keyed on the row and the effective ruleset rather than on the test's age:
    what matters is whether the strongest available evidence path is the one in
    use, which is a question about now, not about ordering.
    """

    if cycle_rulesets.effective(workspace) is None:
        return False
    # Contracted or not. The comparisons a requirement needs are now chosen
    # when its cycle test is written, so an attribute still waiting for them is
    # exactly the one a fallback test was generated over. One the cycle rules
    # could not express was re-routed off ``transaction_cycle`` when that was
    # decided, and does not come back here.
    declares = any(
        isinstance(attribute, dict)
        and attribute.get("evidence_kind") == "transaction_cycle"
        for attribute in row.get("control_attributes") or []
    )
    if not declares or not tests:
        # A row with no test at all is not holding a fallback; it is missing
        # its tests, and saying otherwise sent the reader looking for a test
        # that never existed (a generation that failed outright landed here).
        return False
    return not any(str(item.get("test_kind") or "") == "cycle_vouch" for item in tests)


def _specified_ready(workspace: Workspace, scope: dict) -> Readiness:
    rows = _rows(workspace, scope)
    total = len(rows)
    grouped = _by_row(_scoped_manifest(workspace, scope))
    ready = 0
    review_required = 0
    awaiting_cycle = 0
    for row in rows:
        tests = grouped.get(row["id"], [])
        if _awaits_cycle_test(workspace, row, tests):
            awaiting_cycle += 1
            continue
        if any(item["executable"] for item in tests):
            ready += 1
            continue
        # A linked agent-created draft is missing generation work, not a
        # blocker; an auditor-created draft agent generation cannot overwrite
        # surfaces for review instead of looking like ordinary missing work.
        if any(
            item["status"] == "draft" and item.get("created_by") != "agent"
            for item in tests
        ):
            review_required += 1
    if awaiting_cycle:
        return Readiness(
            "missing",
            (
                f"{counted(awaiting_cycle, 'RCM row')} "
                f"{verb(awaiting_cycle, 'declares', 'declare')} transaction-cycle "
                "evidence and still "
                f"{verb(awaiting_cycle, 'holds', 'hold')} the test generated "
                "before a cycle ruleset was approved",
            ),
            details={"ready": ready, "total": total, "awaiting_cycle": awaiting_cycle},
        )
    if total and ready == total:
        unvouched = _unvouched_types(workspace)
        if unvouched:
            cause, because, ruleset_id = _unvouched_cause(workspace)
            details = {
                "ready": ready,
                "total": total,
                "unvouched_types": unvouched,
                "unvouched_cause": cause,
            }
            if ruleset_id:
                details["ruleset_id"] = ruleset_id
            return Readiness(
                "review_required",
                (
                    f"{counted(len(unvouched), 'document type')} "
                    f"{verb(len(unvouched), 'was', 'were')} extracted against an "
                    f"induced schema ({', '.join(unvouched)}) and no cycle test "
                    f"vouches them; {because}",
                ),
                details=details,
            )
        return Readiness("satisfied", details={"ready": ready, "total": total})
    if not _testable(workspace):
        return Readiness(
            "blocked",
            ("no imported data or documents are available to test against",),
            details={"ready": ready, "total": total},
        )
    if review_required and ready + review_required == total:
        return Readiness(
            "review_required",
            (
                f"{counted(review_required, 'RCM row')} {verb(review_required, 'carries', 'carry')} an auditor-owned test that "
                "cannot be overwritten",
            ),
            details={"ready": ready, "total": total},
        )
    return Readiness(
        "missing",
        (f"{counted(total - ready, 'RCM row')} {verb(total - ready)} at least one executable test",),
        details={"ready": ready, "total": total},
    )


def _generation_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    if not _testable(workspace):
        return []
    grouped = _by_row(_scoped_manifest(workspace, scope))
    force = scope.get("generation_mode") == "force"
    units = []
    for row in _rows(workspace, scope):
        named = _named_test_ids_for_row(workspace, scope, row["id"])
        tests = grouped.get(row["id"], [])
        covered = any(item["executable"] for item in tests)
        upgradeable_draft = any(
            item["status"] == "draft" and item.get("created_by") == "agent"
            for item in tests
        )
        auditor_draft_blocks = any(
            item["status"] == "draft" and item.get("created_by") != "agent"
            for item in tests
        )
        # Naming a test is the instruction. It says the row is not settled
        # whatever the manifest reports, and it says which one test is wrong —
        # so neither the coverage gate nor the auditor-draft gate applies, and
        # force need not be asked for separately.
        if not named:
            if covered and not _awaits_cycle_test(workspace, row, tests):
                if not force and not upgradeable_draft:
                    continue
            elif auditor_draft_blocks:
                # Review-required, not a generation gap the executor can fix
                # without explicit overwrite permission.
                continue
        units.append(
            UnitSpec(
                semantic_unit_id("test_generation", row["id"]),
                "test_generation",
                f"Generate tests — {row.get('risk') or row['id']}",
                (f"rcm:{row['id']}",),
                # The named ids are part of the unit's input identity, so a
                # proposal that rewrote a whole row is never reused as one that
                # rewrites a single test of it.
                {"row": row, "regenerate_test_ids": list(named)} if named else row,
            )
        )
    return units


# --------------------------------------------------------------------------- #
# tests.promoted_from_analysis
# --------------------------------------------------------------------------- #
def _promotion_ready(workspace: Workspace, scope: dict) -> Readiness:
    """Whether every procedure that found something has been answered for.

    Satisfied when nothing is pending, including the case where no analysis
    ever ran: this capability adds no work of its own and must not turn an
    engagement that did no exploratory analysis into an incomplete one.

    Deliberately not scoped by table. A procedure's disposition is a statement
    about the audit's coverage of it, and narrowing an unscoped request to six
    base tables would leave the rest silently unanswered — which is the exact
    shape of the loss this capability exists to close.
    """
    if not workspace.rcm:
        return Readiness(
            "blocked",
            ("no RCM rows exist to fit a promoted procedure to",),
            blocking_on=("planning.rcm_ready",),
        )
    pending = analysis_promotion.candidates(workspace)
    details = {
        "pending": len(pending),
        "exceptions": sum(
            analysis_promotion.exception_count(item) for item in pending
        ),
    }
    if not pending:
        return Readiness("satisfied", details=details)
    return Readiness(
        "missing",
        (
            f"{counted(len(pending), 'saved analysis', 'saved analyses')} holding "
            f"exceptions {verb(len(pending), 'has', 'have')} neither become a test "
            "nor been recorded as declined",
        ),
        details=details,
    )


def _promotion_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    if not workspace.rcm:
        return []
    return [
        UnitSpec(
            semantic_unit_id("analysis_promotion", str(item["id"])),
            "analysis_promotion",
            f"Place analysis — {item.get('title') or item['id']}",
            (f"analysis:{item['id']}",),
            {"analysis_id": str(item["id"])},
        )
        for item in analysis_promotion.candidates(workspace)
    ]


# --------------------------------------------------------------------------- #
# tests.cycle_ruleset_approved
# --------------------------------------------------------------------------- #
def _cycle_attributes(workspace: Workspace) -> list[dict]:
    """The matrix's transaction-cycle attributes — what the cycle rules serve.

    Keyed on the matrix rather than on the extracted records, unlike
    ``_unvouched_types``: this asks what the engagement has *decided* it needs
    linked evidence for. The other asks whether that decision was made at all,
    and the two must not be the same reading or neither can check the other.

    The strategy alone, not the contract. An attribute is contracted when its
    cycle test is written, so requiring ``schema_backed`` here would report no
    demand on exactly the engagements whose tests have not been written yet.
    """

    found: list[dict] = []
    for row in workspace.rcm or []:
        for attribute in row.get("control_attributes") or []:
            if (
                isinstance(attribute, dict)
                and attribute.get("evidence_kind") == "transaction_cycle"
            ):
                found.append(attribute)
    return found


# --------------------------------------------------------------------------- #
# tests.cycle_ruleset_approved
# --------------------------------------------------------------------------- #
#: The agent identity a run records when it approves its own rules. Not a
#: person, and deliberately not shaped like one.
AGENT_APPROVER = "agent:auto-mode"


def _auto_mode(scope: dict) -> bool:
    """Whether this run may approve what it proposed.

    Defaults to *permission* when the key is absent, which is every call from
    outside a run — the status endpoint, engagement progress, a readiness read
    in a test. Outside a run there is no delegation in force, so the standing
    state of a workspace holding an unapproved proposal is "waiting for an
    auditor", and that is what those callers should show.
    """

    return scope.get("permission_mode") is False


def _pending_ruleset(workspace: Workspace) -> dict | None:
    """The proposal an approval would make effective, if one is waiting."""

    if cycle_rulesets.effective(workspace) is not None:
        return None
    pending = [
        record
        for record in cycle_rulesets.list_rulesets(workspace)
        if str(record.get("status")) == "proposed"
    ]
    return pending[-1] if pending else None


def _ruleset_approval_ready(workspace: Workspace, scope: dict) -> Readiness:
    """Whether the cycle's rules are effective.

    The cycle design proposes rules whenever the engagement's documents can be
    linked, whether or not the matrix has asked for linked evidence yet. A
    proposal waiting is therefore always something to approve; where none is
    waiting and the matrix asks nothing of the cycle, there is nothing to wait
    for.

    The two modes part on a waiting proposal. In ``permission`` this stays what
    the design intended — a human gate, reported and never actioned, whose
    unmet state is a fact about the workspace rather than work the run can do.
    In ``auto`` the auditor has delegated the run's approvals, so an unapproved
    proposal is work, and the capability expands a unit to do it.
    """

    if cycle_rulesets.effective(workspace) is not None:
        return Readiness("satisfied")
    pending = _pending_ruleset(workspace)
    if pending is None and not _cycle_attributes(workspace):
        return Readiness("satisfied")
    if pending is None:
        # Asked for and nothing waiting: the cycle design found nothing it could
        # link, or the auditor rejected what it did. Approving is not the
        # repair in either mode, so this reports and expands nothing.
        return Readiness(
            "review_required",
            (
                "the matrix asks for transaction-cycle evidence and every "
                "proposed cycle ruleset was rejected"
                if cycle_rulesets.list_rulesets(workspace)
                else "the matrix asks for transaction-cycle evidence and the "
                "cycle design proposed no rules to approve",
            ),
        )
    if not _auto_mode(scope):
        return Readiness(
            "review_required",
            ("the cycle rules are proposed and await an auditor's approval",),
            details={"ruleset_id": str(pending.get("ruleset_id") or "")},
        )
    return Readiness(
        "missing",
        ("the cycle rules are proposed and not yet effective",),
        details={"ruleset_id": str(pending.get("ruleset_id") or "")},
    )


def _ruleset_approval_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    """One unit, and only where the run was given the authority to run it.

    Permission mode expands nothing: a stage with no units settles from its own
    readiness, so the gate reports and the run carries on to generate whatever
    tests it can without the cycle — which is exactly what it did before this
    capability existed.

    The proposal's hash rides on the input payload so that approving is bound to
    the rules that were actually read. A proposal re-expanded against moved
    schemas is a different unit, not a repeat of this one.
    """

    if not _auto_mode(scope):
        return []
    pending = _pending_ruleset(workspace)
    if pending is None:
        return []
    ruleset_id = str(pending.get("ruleset_id") or "")
    return [
        UnitSpec(
            semantic_unit_id("cycle_ruleset_approval", ruleset_id),
            "cycle_ruleset_approval",
            "Approve the cycle rules for this engagement",
            (f"cycle_ruleset:{ruleset_id}",),
            {
                "ruleset_id": ruleset_id,
                "ruleset_hash": str(pending.get("ruleset_hash") or ""),
            },
        )
    ]


def _cycle_ruleset_approved() -> Capability:
    return Capability(
        "tests.cycle_ruleset_approved",
        "cycle_ruleset_approval",
        "Cycle rules made effective",
        "cycle_ruleset_approval",
        audit_workflow.dependencies("tests.cycle_ruleset_approved"),
        _ruleset_approval_ready,
        _ruleset_approval_units,
        # No context and no worker: approving reads the stored proposal and the
        # corpus it was measured against. There is no question here for a model
        # — the judgement the gate exists for was made when the auditor chose
        # the mode, and re-asking a model to bless its own rules would add a
        # ceremony rather than a check.
        barrier="all_settled_then_validate",
        # The rules are the cycle design's, proposed in the same commit as the
        # shape. A run that redesigns the cycle has proposed new rules, and an
        # approval of the old ones is not an approval of these.
        invalidate_on=("planning:cycle",),
    
        produces=("ruleset",),
        accepts_refs=(),
    )


def _analysis_promoted() -> Capability:
    return Capability(
        "tests.promoted_from_analysis",
        "analysis_promotion",
        "Analyses placed in the matrix",
        "analysis_promotion",
        audit_workflow.dependencies("tests.promoted_from_analysis"),
        _promotion_ready,
        _promotion_units,
        context="analysis.promotion",
        # One unit is one saved procedure. The units read no shared state and
        # each commits under its own analysis' parent hash, so the ordering
        # between them is immaterial and the stage is free to fan out.
        barrier="all_settled_parallel",
        # A rewritten RCM invalidates every fit: a procedure placed against a
        # row that no longer exists is a placement nobody made.
        invalidate_on=("rcm",),
    
        produces=("datatest",),
        accepts_refs=("table",),
    )


def _tests_specified() -> Capability:
    return Capability(
        "tests.specified",
        "test_specs",
        "Executable test specifications",
        "test_generation",
        audit_workflow.dependencies("tests.specified"),
        _specified_ready,
        _generation_units,
        context="tests.generate",
        # One unit is one RCM row: the rows are independent, the turn reads no
        # other unit's output, and the commit guards exactly its own row's parent
        # hash against a freshly read workspace, so nothing here depends on a
        # sibling having landed first. Serialized, a row-per-unit expansion is
        # also the one capability that cannot finish inside the run budget —
        # seventy turns at a minute each exhaust it before the stage completes.
        barrier="all_settled_parallel",
        invalidate_on=("rcm",),
    
        produces=("datatest", "doctest"),
        accepts_refs=("rcm", "datatest", "doctest"),
        redoes_named=("datatest", "doctest"),
    )


_BUILDERS = {
    "tests.cycle_ruleset_approved": _cycle_ruleset_approved,
    "tests.specified": _tests_specified,
    "tests.promoted_from_analysis": _analysis_promoted,
}


def capabilities() -> tuple[Capability, ...]:
    """Return this group's capability declarations in authoritative order."""

    return tuple(_BUILDERS[capability_id]() for capability_id in CAPABILITY_IDS)
