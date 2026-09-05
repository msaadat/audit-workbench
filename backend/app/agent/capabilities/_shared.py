"""Shared selectors and artifact hashes for the grouped audit capability modules.

The planning, fieldwork, and reporting capability groups all narrow the RCM by
requested scope and select eligible observations, so that logic lives here once
rather than being duplicated per group.

The material artifact hashes below are the audit domain's provenance identities.
Registered executors stamp them on committed artifacts (``workflow_parent_sha1``,
``workflow_basis_sha1``) and compare them during interrupted-commit
reconciliation. They deliberately do **not** drive readiness or scheduling:
readiness is existence and structural usability only, and the auditor decides
when to regenerate.
"""

from __future__ import annotations

from dataclasses import dataclass

from ... import methodology
from ...workspaces import Workspace
from ..workflow import UnitSpec, canonical_sha1


# Mirrors ``workspace_transactions._RCM_MATERIAL_FIELDS``, including its
# omission of ``review_status``: sign-off says who has read the row, not what
# the row asserts, so it must not invalidate work generated from the row's
# risk-and-control definition.
_RCM_MATERIAL_FIELDS = (
    "id", "process", "risk", "risk_rating", "business_cycle",
    "control_attributes", "control",
    "control_type", "control_owner", "criteria", "criteria_refs",
    "evidence_refs",
)


def _rcm_material_projection(row: dict | None) -> dict | None:
    """Return the auditor-editable RCM fields that define a row's basis.

    Generated test links, execution rollups, and finding links are deliberately
    excluded: they are workflow output, not source material for a new run.
    Keeping this small projection in the capability layer avoids coupling
    declarative readiness code to the transaction/write subsystem.
    """

    if row is None:
        return None
    return {field: row.get(field) for field in _RCM_MATERIAL_FIELDS}


def planning_basis_sha1(workspace: Workspace) -> str:
    table_signatures = {}
    for name in workspace.table_names():
        try:
            table_signatures[name] = workspace._table_signature(name)
        except Exception as error:
            # Broken/missing sources still participate deterministically in
            # invalidation; readiness checks must not crash the scheduler.
            table_signatures[name] = {"unavailable": type(error).__name__, "message": str(error)}
    return canonical_sha1(
        {
            "context": workspace.planning.get("context") or {},
            "tables": table_signatures,
            "documents": [
                {
                    key: item.get(key)
                    for key in ("id", "sha1", "title", "category", "text_state")
                }
                for item in workspace.documents
            ],
            "methodology": [
                {key: item.get(key) for key in ("id", "scope", "version", "sha1")}
                for item in methodology.list_packs(workspace)
            ],
        }
    )


def apm_sha1(workspace: Workspace) -> str:
    return canonical_sha1(
        {
            "markdown": workspace.planning.get("apm_markdown") or "",
            "basis": workspace.planning.get("workflow_basis_sha1"),
        }
    )


def rcm_row_sha1(row: dict) -> str:
    return canonical_sha1(_rcm_material_projection(row))



#: Ref prefixes a request may name, mapped to the field of :class:`TargetScope`
#: they fill. ``workspace:current`` is the absence of a target rather than one
#: of them, and is deliberately not here.
_REF_KINDS: dict[str, str] = {
    "rcm": "rcm_ids",
    "datatest": "test_ids",
    "doctest": "test_ids",
    "observation": "observation_ids",
    "finding": "finding_ids",
    "document": "document_ids",
}


def test_rcm_id(workspace: Workspace, test_id: str) -> str | None:
    """The RCM row one test is linked to, or None when it is unlinked.

    Delegated rather than moved here: ``findings`` needs the same answer to
    validate a draft's links, and a domain module importing the capability
    layer would invert the direction every other module runs in.
    """

    from ... import findings

    return findings._test_rcm_id(workspace, test_id)


@dataclass(frozen=True)
class TargetScope:
    """What a request named, resolved to the things a capability expands over.

    A request has been able to name an RCM row or an observation for as long as
    there has been a workflow, which meant the smallest thing an auditor could
    address was a whole row: "test DT-123 doesn't look right, redraft it" could
    be scoped to the row DT-123 sits on and no finer, so the generation stage
    either found nothing to do or, under force, regenerated every test the row
    had. Naming the test itself is the point of this type.

    ``rcm_ids`` is the union of the rows named directly and the rows reached
    through a named test, observation or finding, so every existing caller that
    only understands rows keeps working against a finer request.
    """

    rcm_ids: tuple[str, ...] = ()
    test_ids: tuple[str, ...] = ()
    observation_ids: tuple[str, ...] = ()
    finding_ids: tuple[str, ...] = ()
    document_ids: tuple[str, ...] = ()
    explicit: bool = False


def target_scope(workspace: Workspace, scope: dict) -> TargetScope:
    """Resolve a request's ``target_refs`` to the artifacts they name.

    Unparseable and unknown refs are dropped rather than raising: a scope is a
    narrowing, and a ref naming something that no longer exists narrows to
    nothing, which the caller reports as work it cannot find. Raising here
    would turn a stale button into a failed run.
    """

    named: dict[str, list[str]] = {field: [] for field in set(_REF_KINDS.values())}
    for value in (str(item) for item in scope.get("target_refs") or []):
        kind, separator, identifier = value.partition(":")
        field = _REF_KINDS.get(kind) if separator else None
        if field and identifier:
            named[field].append(identifier)

    def unique(values: list[str]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(values))

    test_ids = unique(named["test_ids"])
    observation_ids = unique(named["observation_ids"])
    finding_ids = unique(named["finding_ids"])
    document_ids = unique(named["document_ids"])

    # Everything below a row still selects the row it belongs to, so a stage
    # that expands per row keeps working when the request named a test.
    rows_from_tests = [
        row_id
        for test_id in test_ids
        if (row_id := test_rcm_id(workspace, test_id))
    ]
    observations = {
        str(item.get("id") or ""): item
        for item in getattr(workspace, "observations", ())
    }
    rows_from_observations = [
        str(observations[observation_id].get("rcm_id") or "")
        for observation_id in observation_ids
        if observation_id in observations
    ]
    finding_observations = {
        str(item.get("id") or ""): str(item.get("source_observation_id") or "")
        for item in getattr(workspace, "findings", ())
    }
    rows_from_findings = [
        str(observations[observation_id].get("rcm_id") or "")
        for finding_id in finding_ids
        if (observation_id := finding_observations.get(finding_id))
        and observation_id in observations
    ]
    rcm_ids = unique(
        [
            value
            for value in [
                *named["rcm_ids"],
                *rows_from_tests,
                *rows_from_observations,
                *rows_from_findings,
            ]
            if value
        ]
    )
    return TargetScope(
        rcm_ids=rcm_ids,
        test_ids=test_ids,
        observation_ids=observation_ids,
        finding_ids=finding_ids,
        document_ids=document_ids,
        explicit=bool(
            named["rcm_ids"]
            or test_ids
            or observation_ids
            or finding_ids
            or document_ids
        ),
    )


def target_rcm_ids(workspace: Workspace, scope: dict) -> list[str]:
    """RCM row IDs in the requested scope (all rows when no target is given).

    Ordered by the matrix, not by the request: a stage expands one unit per row
    and the order it expands them in is the order the auditor reads them in.
    """

    selected = target_scope(workspace, scope)
    # An explicit request that resolves to no known row selects nothing, which
    # is what "regenerate DT-123" must mean when DT-123 has been deleted. A
    # request naming only documents is a different case: it says nothing about
    # rows, so the row scope stays open.
    narrows_rows = bool(
        selected.rcm_ids
        or selected.test_ids
        or selected.observation_ids
        or selected.finding_ids
    )
    if not narrows_rows:
        return [row["id"] for row in workspace.rcm]
    chosen = set(selected.rcm_ids)
    return [row["id"] for row in workspace.rcm if row["id"] in chosen]


def named_test_ids_for_row(
    workspace: Workspace, scope: dict, rcm_id: str
) -> tuple[str, ...]:
    """The tests on one row that the request named by id, in request order.

    The single answer to "which tests did the auditor point at", shared by the
    capability that expands the unit and the binder that builds its target, so
    the two cannot disagree about what a run was asked to redraft. Deriving it
    twice from the durable scope is deliberate: the unit record carries only its
    input *hash*, and widening it to carry the payload would put a whole RCM row
    in ``run.json`` for every generation unit ever expanded.
    """

    selected = target_scope(workspace, scope)
    return tuple(
        test_id
        for test_id in selected.test_ids
        if test_rcm_id(workspace, test_id) == str(rcm_id)
    )


def rows(workspace: Workspace, scope: dict) -> list[dict]:
    """RCM rows selected by the requested scope."""

    selected = set(target_rcm_ids(workspace, scope))
    return [row for row in workspace.rcm if row["id"] in selected]


def all_tests(workspace: Workspace) -> list[dict]:
    """Every durable test in the workspace, linked or not."""

    from ... import doc_tests

    return [
        *workspace.data_tests,
        *(
            doc_tests.load_test(workspace, summary["id"])
            for summary in doc_tests.list_tests(workspace)
        ),
    ]


def named_observation_ids(workspace: Workspace, scope: dict) -> tuple[str, ...]:
    """Observations the request pointed at, directly or through a finding.

    Naming a finding is naming the observation it was drafted from: a finding
    has no existence apart from its source observation, and the capability that
    would redraft it expands per observation.
    """

    selected = target_scope(workspace, scope)
    sources = {
        str(item.get("id") or ""): str(item.get("source_observation_id") or "")
        for item in getattr(workspace, "findings", ())
    }
    return tuple(
        dict.fromkeys(
            [
                *selected.observation_ids,
                *(
                    observation_id
                    for finding_id in selected.finding_ids
                    if (observation_id := sources.get(finding_id))
                ),
            ]
        )
    )


def scoped_observations(workspace: Workspace, scope: dict) -> list[dict]:
    """Observations in the explicit observation, finding, or RCM scope."""

    named = set(named_observation_ids(workspace, scope))
    if named:
        return [
            item for item in workspace.observations
            if str(item.get("id") or "") in named
        ]
    selected_rcm_ids = set(target_rcm_ids(workspace, scope))
    return [
        item for item in workspace.observations
        if item.get("rcm_id") in selected_rcm_ids
    ]


def eligible_observations(workspace: Workspace, scope: dict | None = None) -> list[dict]:
    """Exception observations in scope that may become finding drafts."""

    return [
        item
        for item in scoped_observations(workspace, scope or {})
        if item.get("outcome") == "exception"
    ]


def single_unit(kind: str, title: str, *parents: str):
    """A one-unit expansion for a whole-workspace capability."""

    return lambda _workspace, _scope: [
        UnitSpec(kind, kind, title, tuple(parents), parents)
    ]
