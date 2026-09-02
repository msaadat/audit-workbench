"""Read-only provenance: what the agent read, proposed, and committed.

Nothing here mutates, and nothing here returns worker content. See
:mod:`app.provenance` for the two rules the payload keeps.
"""

from __future__ import annotations

from fastapi import APIRouter, Query

from .. import provenance, workspaces

router = APIRouter(prefix="/api/workspaces/{workspace_id}", tags=["provenance"])


@router.get("/agent/runs/{run_id}/units/{unit_id:path}/provenance")
def get_unit_provenance(workspace_id: str, run_id: str, unit_id: str):
    ws = workspaces.load_workspace(workspace_id)
    return provenance.unit_provenance(ws, run_id, unit_id)


@router.get("/provenance")
def get_artifact_provenance(
    workspace_id: str,
    artifact: str = Query(..., description="Artifact ref, e.g. planning:apm or rcm:RCM-9FB041"),
):
    """Provenance for whichever unit last committed this artifact.

    Answers "who wrote this, and what did they read" for any work product,
    including one no agent ever touched — which reports itself as
    `unattributed` rather than as an empty trail.
    """
    ws = workspaces.load_workspace(workspace_id)
    return provenance.artifact_provenance(ws, artifact)
