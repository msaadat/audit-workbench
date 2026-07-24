"""Deterministic executors for audit fieldwork/roll-up capabilities.

These executors perform no model calls. ``roll_up_results`` recomputes each RCM
row's derived result and its observations from the current execution artifacts and
commits only material changes; it binds through the scheduler's deterministic
execution path for ``results.rolled_up``. As with the reporting siblings, the
observation/disposition auditor judgment is a declared checkpoint that runs
between roll-up and finding creation, not part of this executor.
"""

from __future__ import annotations

from ... import rcm_execution
from ...workspaces import Workspace

RESULT_REF_PREFIX = "rcm"


def result_ref(rcm_id: str) -> str:
    """The stable artifact reference for one rolled-up RCM row result."""

    return f"{RESULT_REF_PREFIX}:{rcm_id}"


def roll_up_results(workspace: Workspace) -> list[str]:
    """Recompute RCM results and observations; return stable per-row refs.

    Deterministic and self-committing: ``rcm_execution.rollup`` recomputes every
    RCM row's derived result and its observations from the current execution
    artifacts and persists only material changes. Observation identities are keyed
    on ``execution_ref``, so a repeated roll-up reuses the same observation rows
    rather than creating duplicates — the result and observation identities stay
    stable across runs. No model call is involved; the auditor's observation
    disposition runs as a declared checkpoint before finding creation, not here.
    """

    result = rcm_execution.rollup(workspace)
    return [result_ref(row["rcm_id"]) for row in result["rows"]]


__all__ = ["RESULT_REF_PREFIX", "result_ref", "roll_up_results"]
