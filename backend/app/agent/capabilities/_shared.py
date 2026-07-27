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

from ... import methodology
from ...workspaces import Workspace
from ..workflow import UnitSpec, canonical_sha1


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
    return canonical_sha1(
        {
            key: row.get(key)
            for key in (
                "id", "process", "risk", "risk_rating", "assertion", "control",
                "control_type", "control_owner", "criteria", "criteria_refs",
                "evidence_refs", "review_status",
            )
        }
    )


# Dispositions that make an observation eligible to become a finding draft.
ELIGIBLE_DISPOSITIONS = frozenset(
    {"confirmed_control_exception", "draft_finding_candidate"}
)


def target_rcm_ids(workspace: Workspace, scope: dict) -> list[str]:
    """RCM row IDs in the requested scope (all rows when no target is given)."""

    refs = [str(value) for value in scope.get("target_refs") or []]
    selected = {
        value.split(":", 1)[1]
        for value in refs
        if value.startswith("rcm:") and ":" in value
    }
    return [row["id"] for row in workspace.rcm if not selected or row["id"] in selected]


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


def eligible_observations(workspace: Workspace) -> list[dict]:
    """Disposed observations whose disposition makes them finding-eligible."""

    return [
        item
        for item in workspace.observations
        if item.get("status") == "disposed"
        and item.get("disposition") in ELIGIBLE_DISPOSITIONS
    ]


def single_unit(kind: str, title: str, *parents: str):
    """A one-unit expansion for a whole-workspace capability."""

    return lambda _workspace, _scope: [
        UnitSpec(kind, kind, title, tuple(parents), parents)
    ]
