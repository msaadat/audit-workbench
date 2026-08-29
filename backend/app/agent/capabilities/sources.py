"""Sources capability group of the audit workflow.

Owns ``sources.imported``: whether this engagement has anything to audit.

It is the head of the graph and the one capability the agent never performs.
Importing is the auditor's act — a folder dropped on the shell, staged and
classified before anything is added — so this capability expands no units and
resolves no worker. What it contributes is the *edge*: planning depends on it,
so an engagement holding no data and no documents reports planning as blocked
rather than offering to write a memorandum about nothing.

The readiness below was previously inlined in ``planning.context_ready``, which
meant the condition existed without a stage to hang on. The record could not
draw it, the graph could not order against it, and a new workspace's first
suggestion was to draft the APM.
"""

from __future__ import annotations

from ...workspaces import Workspace
from ..workflow import Capability, Readiness, UnitSpec
from ..workflows import audit as audit_workflow

CAPABILITY_IDS: tuple[str, ...] = ("sources.imported",)


def _sources_imported(workspace: Workspace, _scope: dict) -> Readiness:
    """Whether the engagement holds anything to audit.

    Either kind satisfies it. A data-only engagement and a document-only one are
    both real, and the stages downstream expand no units for whichever kind is
    absent, so requiring both would block work that can genuinely proceed.
    """
    if workspace.documents or workspace.tables:
        return Readiness("satisfied")
    return Readiness("missing", ("no data or documents have been imported",))


def _sources_units(_workspace: Workspace, _scope: dict) -> list[UnitSpec]:
    """Never any. The agent cannot import; it can only wait to be given.

    A capability whose readiness is satisfied and whose units are empty passes
    straight through the scheduler, which is the same way an audit carrying no
    documents already passes the document capabilities.
    """
    return []


def _sources_imported_capability() -> Capability:
    return Capability(
        "sources.imported",
        "sources",
        "Sources",
        "sources",
        audit_workflow.dependencies("sources.imported"),
        _sources_imported,
        _sources_units,
        # No model ever sees this capability: it reads two counts.
        context=None,
        invalidate_on=("sources",),
    )


def capabilities() -> tuple[Capability, ...]:
    return (_sources_imported_capability(),)
