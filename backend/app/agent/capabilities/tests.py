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
    cycle_linking,
    cycle_measurement,
    cycle_rulesets,
    document_schemas,
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
    "tests.cycle_ruleset_proposed",
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
            "the matrix asks for transaction-cycle evidence and no cycle "
            "ruleset has been proposed",
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
    declares = any(
        isinstance(attribute, dict)
        and cycle_linking.schema_backed(attribute)
        and attribute.get("evidence_kind") == "transaction_cycle"
        for attribute in row.get("control_attributes") or []
    )
    if not declares:
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
                "evidence and still hold the test generated before a cycle "
                "ruleset was approved",
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
# tests.cycle_ruleset_proposed
# --------------------------------------------------------------------------- #
def _cycle_attributes(workspace: Workspace) -> list[dict]:
    """The matrix's transaction-cycle attributes — what a ruleset exists to serve.

    Keyed on the matrix rather than on the extracted records, unlike
    ``_unvouched_types``: this asks what the engagement has *decided* it needs
    linked evidence for, which is the question a proposal answers. The other
    asks whether that decision was made at all, and the two must not be the
    same reading or neither can check the other.

    The strategy alone, not the contract. A matrix commits its cycle attributes
    uncontracted and this stage is what contracts them, so requiring
    ``schema_backed`` here would report the stage satisfied on exactly the
    engagements that need it and propose for none of them.
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


def _ruleset_ready(workspace: Workspace, scope: dict) -> Readiness:
    """Whether this engagement has the cycle rules its matrix asks for.

    Satisfied where nothing asks: an engagement whose matrix classifies no
    attribute as transaction-cycle evidence needs no rules, and blocking it on
    a proposal nobody will read would make every audit wait on a cycle it does
    not have. Where the matrix *does* ask, an unproposed ruleset is missing
    work the agent can do — as distinct from the approval that follows it,
    which it cannot.
    """

    if not _cycle_attributes(workspace):
        return Readiness("satisfied")
    if not document_schemas.list_schemas(workspace):
        # The vocabulary a proposal is written against does not exist yet.
        # Reported rather than blocking: classification and induction are the
        # repair, and they are their own capabilities.
        return Readiness(
            "review_required",
            (
                "The matrix asks for transaction-cycle evidence and no document "
                "schema has been induced, so no cycle rules can be written.",
            ),
        )
    if cycle_rulesets.list_rulesets(workspace):
        return Readiness("satisfied")
    return Readiness(
        "missing",
        ("No cycle ruleset has been proposed for this engagement.",),
    )


def _ruleset_units(workspace: Workspace, scope: dict) -> list[UnitSpec]:
    """One unit, because a workspace holds one cycle.

    The guarded parents are the matrix rows whose attributes ask for linked
    evidence: those are the question the rules answer, and a rewritten row must
    conflict a proposal written against the old one.

    Schema staleness is carried by the input payload instead, because a schema
    is not a workspace artifact and ``parent_hashes`` has nothing to say about
    a side store — the same reason the schema freeze posts its own content hash.
    Stamping each schema's hash here means a re-derived schema moves the unit's
    ``input_sha1`` and the proposal re-expands, rather than an auditor
    approving rules against a vocabulary that has since moved.
    """

    if not _cycle_attributes(workspace):
        return []
    schemas = document_schemas.list_schemas(workspace)
    if not schemas or cycle_rulesets.list_rulesets(workspace):
        return []
    rows = tuple(
        f"rcm:{row['id']}"
        for row in workspace.rcm or []
        if any(
            isinstance(attribute, dict)
            and attribute.get("evidence_kind") == "transaction_cycle"
            for attribute in row.get("control_attributes") or []
        )
    )
    vocabulary = sorted(
        (
            {
                "document_type": str(item.get("document_type") or ""),
                "schema_hash": str(item.get("schema_hash") or ""),
            }
            for item in schemas
            if item.get("document_type")
        ),
        key=lambda item: item["document_type"],
    )
    # The shape joins the matrix rows as a guarded parent: the roles are its,
    # so a reshaped cycle must conflict a proposal written against the positions
    # it used to declare, exactly as a re-derived schema does for the fields.
    parents = (*rows, *(("planning:cycle",) if workspace.planning.get("cycle") else ()))
    return [
        UnitSpec(
            semantic_unit_id("cycle_ruleset", "proposal"),
            "cycle_ruleset_proposal",
            "Propose the cycle rules for this engagement",
            parents,
            {"schemas": vocabulary},
        )
    ]


def _cycle_ruleset_proposed() -> Capability:
    return Capability(
        "tests.cycle_ruleset_proposed",
        "cycle_ruleset",
        "Cycle rules proposed for review",
        "cycle_ruleset_proposal",
        audit_workflow.dependencies("tests.cycle_ruleset_proposed"),
        _ruleset_ready,
        _ruleset_units,
        # The schemas travel as declared context, so their provenance is
        # recorded like every other supplied source. Their *identity* rides on
        # the unit's input payload instead — the hashes are stamped there, so a
        # re-derived schema moves ``input_sha1`` and re-proposes rather than
        # leaving an auditor approving rules against a vocabulary that moved.
        context="tests.cycle_linkage",
        # One unit, and it commits. Serialized for the same reason every
        # committing capability is.
        barrier="all_settled_then_validate",
        # Rules are written to answer the matrix's comparisons, so a rewritten
        # matrix is a different question and the proposal answering the old one
        # is not an answer to it.
        invalidate_on=("rcm",),
    )


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
    """Whether the rules this engagement needs are effective.

    Satisfied where nothing asks, for the same reason proposing is: an
    engagement whose matrix classifies no attribute as transaction-cycle needs
    no rules and must not wait on rules for one.

    Where the matrix does ask, the two modes part. In ``permission`` this stays
    what the design intended — a human gate, reported and never actioned, whose
    unmet state is a fact about the workspace rather than work the run can do.
    In ``auto`` the auditor has delegated the run's approvals, so an unapproved
    proposal is work, and the capability expands a unit to do it.
    """

    if not _cycle_attributes(workspace):
        return Readiness("satisfied")
    if cycle_rulesets.effective(workspace) is not None:
        return Readiness("satisfied")
    pending = _pending_ruleset(workspace)
    if not _auto_mode(scope):
        return Readiness(
            "review_required",
            (
                "the cycle rules are proposed and await an auditor's approval"
                if pending
                else "the matrix asks for transaction-cycle evidence and no "
                "cycle ruleset is effective",
            ),
            details={"ruleset_id": str((pending or {}).get("ruleset_id") or "")},
        )
    return Readiness(
        "missing",
        (
            "the cycle rules are proposed and not yet effective"
            if pending
            else "no cycle ruleset has been proposed to approve",
        ),
        details={"ruleset_id": str((pending or {}).get("ruleset_id") or "")},
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

    if not _auto_mode(scope) or not _cycle_attributes(workspace):
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
        # Rules answer the matrix's comparisons; a rewritten matrix is a
        # different question, and an approval of the answer to the old one is
        # not an approval of the answer to this one.
        invalidate_on=("rcm",),
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
    )


_BUILDERS = {
    "tests.cycle_ruleset_proposed": _cycle_ruleset_proposed,
    "tests.cycle_ruleset_approved": _cycle_ruleset_approved,
    "tests.specified": _tests_specified,
    "tests.promoted_from_analysis": _analysis_promoted,
}


def capabilities() -> tuple[Capability, ...]:
    """Return this group's capability declarations in authoritative order."""

    return tuple(_BUILDERS[capability_id]() for capability_id in CAPABILITY_IDS)
