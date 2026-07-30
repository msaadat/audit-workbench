"""Provenance reads the sidecars back without inventing or leaking anything.

Two properties matter more than the payload's shape. A trail that fills its own
gaps is worse than no trail, so every sidecar reports its own state and a
damaged one is never silently downgraded. And provenance must not become a
second way to read the work product, so no worker content crosses the boundary.
"""

from __future__ import annotations

import json

import pytest

from app import provenance
from app.agent import store
from app.agent.context import manifest as context_manifest
from app.agent.context.model import (
    ContextManifest,
    ContextRepresentation,
    ContextSelection,
    ContextSize,
)
from app.workspaces import WorkspaceError


def _run_with_unit(workspace, *, unit_id="apm", capability="planning.apm_ready"):
    """A minimal workflow run carrying one unit, with no sidecars yet."""
    run = store.new_command_run(
        workspace, "auto", {"source": "chat", "text": "Draft the APM"}
    )
    run["workflow"] = {
        "definition": "audit_workflow_v3",
        "stages": [{
            "id": "apm",
            "capability": capability,
            "title": "Audit planning memorandum",
            "status": "succeeded",
            "units": [{
                "id": unit_id,
                "kind": "apm",
                "title": "Draft audit planning memorandum",
                "capability": capability,
                "status": "succeeded",
                "attempts": 1,
                "context_manifest": None,
                "proposal_sidecar": None,
                "receipt_sidecar": None,
                "started_at": None,
                "finished_at": None,
                "error": None,
            }],
        }],
    }
    store.save_run(workspace, run)
    return run


def _manifest(unit_id="apm", capability="planning.apm_ready"):
    return ContextManifest(
        capability_id=capability,
        unit_id=unit_id,
        context_spec_hash="sha256:" + "a" * 64,
        resolver_hash="sha256:" + "b" * 64,
        selections=(
            ContextSelection(
                source_id="policy",
                source_type="document",
                source_ref="document:D1",
                source_hash="sha256:" + "c" * 64,
                selector_kind="deterministic",
                selector_id="documents.policies",
                selector_definition_hash="sha256:" + "d" * 64,
                reason="Matched the declared local metadata constraints.",
                representation=ContextRepresentation(kind="document_text", options={}),
                supplied_size=ContextSize(items=1, characters=1200, estimated_tokens=300),
            ),
        ),
        omissions=(),
        truncations=(),
        privacy_decisions=(),
        supplied_size=ContextSize(items=1, characters=1200, estimated_tokens=300),
    )


# --------------------------------------------------------------------------- #
# Fail closed
# --------------------------------------------------------------------------- #
def test_a_unit_with_no_sidecars_reports_each_one_absent(workspace_with_data):
    run = _run_with_unit(workspace_with_data)
    payload = provenance.unit_provenance(workspace_with_data, run["id"], "apm")

    assert payload["context"]["state"] == "absent"
    assert payload["proposal"]["state"] == "absent"
    assert payload["receipt"]["state"] == "absent"
    # An absent sidecar still says why, so the rail never renders a blank.
    assert payload["context"]["reason"]


def test_a_reference_to_a_missing_manifest_file_is_unavailable(workspace_with_data):
    run = _run_with_unit(workspace_with_data)
    manifest = _manifest()
    reference = context_manifest.persist_manifest(workspace_with_data, run["id"], manifest)
    run["workflow"]["stages"][0]["units"][0]["context_manifest"] = reference
    store.save_run(workspace_with_data, run)

    # Delete the file the reference points at.
    (store.run_dir(workspace_with_data, run["id"]) / reference["path"]).unlink()
    payload = provenance.unit_provenance(workspace_with_data, run["id"], "apm")

    assert payload["context"]["state"] == "unavailable"
    assert payload["context"]["reason"]
    assert "selections" not in payload["context"], "a broken manifest must not be partly reported"


def test_a_manifest_whose_content_no_longer_matches_its_hash_is_invalid(workspace_with_data):
    run = _run_with_unit(workspace_with_data)
    reference = context_manifest.persist_manifest(workspace_with_data, run["id"], _manifest())
    run["workflow"]["stages"][0]["units"][0]["context_manifest"] = reference
    store.save_run(workspace_with_data, run)

    # Tamper with the persisted manifest so its identity no longer matches.
    path = store.run_dir(workspace_with_data, run["id"]) / reference["path"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["selections"][0]["reason"] = "Something else entirely"
    path.write_text(json.dumps(payload), encoding="utf-8")

    result = provenance.unit_provenance(workspace_with_data, run["id"], "apm")
    assert result["context"]["state"] == "invalid"
    assert "selections" not in result["context"]


def test_an_unknown_unit_raises_rather_than_returning_an_empty_trail(workspace_with_data):
    run = _run_with_unit(workspace_with_data)
    with pytest.raises(WorkspaceError, match="not part of run"):
        provenance.unit_provenance(workspace_with_data, run["id"], "no-such-unit")


def test_every_sidecar_state_is_one_of_the_declared_four(workspace_with_data):
    run = _run_with_unit(workspace_with_data)
    payload = provenance.unit_provenance(workspace_with_data, run["id"], "apm")
    for key in ("context", "proposal", "receipt"):
        assert payload[key]["state"] in provenance.STATES


# --------------------------------------------------------------------------- #
# No worker content crosses the boundary
# --------------------------------------------------------------------------- #
def test_the_proposal_body_is_never_returned(workspace_with_data):
    from app.agent.runtime.unit_pipeline import UnitSidecarStore

    run = _run_with_unit(workspace_with_data)
    secret = "CONFIDENTIAL DRAFT MEMORANDUM BODY"
    sidecars = UnitSidecarStore(workspace_with_data, run["id"])
    reference = sidecars.persist_proposal("apm", {"apm_markdown": secret})
    run["workflow"]["stages"][0]["units"][0]["proposal_sidecar"] = reference
    store.save_run(workspace_with_data, run)

    payload = provenance.unit_provenance(workspace_with_data, run["id"], "apm")
    assert payload["proposal"]["state"] == "available"
    assert payload["proposal"]["content_withheld"] is True
    assert payload["proposal"]["payload_hash"] == reference["payload_hash"]
    assert secret not in json.dumps(payload), "the generated artifact must not leak through provenance"


def test_the_manifest_passes_through_because_it_is_content_free(workspace_with_data):
    run = _run_with_unit(workspace_with_data)
    reference = context_manifest.persist_manifest(workspace_with_data, run["id"], _manifest())
    run["workflow"]["stages"][0]["units"][0]["context_manifest"] = reference
    store.save_run(workspace_with_data, run)

    context = provenance.unit_provenance(workspace_with_data, run["id"], "apm")["context"]
    assert context["state"] == "available"
    assert context["manifest_hash"] == reference["manifest_hash"]
    assert len(context["selections"]) == 1
    selection = context["selections"][0]
    # A selection records identity and size, never the text itself.
    assert selection["source_ref"] == "document:D1"
    assert selection["supplied_size"]["characters"] == 1200
    assert "content" not in selection and "text" not in selection


# --------------------------------------------------------------------------- #
# Attribution
# --------------------------------------------------------------------------- #
def test_an_artifact_no_agent_wrote_is_reported_unattributed(workspace_with_data):
    payload = provenance.artifact_provenance(workspace_with_data, "planning:apm")
    assert payload["state"] == "unattributed"
    assert payload["artifact_ref"] == "planning:apm"
    assert payload["reason"]


def test_an_empty_artifact_ref_resolves_to_nothing(workspace_with_data):
    assert provenance.resolve_artifact(workspace_with_data, "") is None
    assert provenance.resolve_artifact(workspace_with_data, "   ") is None


def test_an_artifact_resolves_to_the_unit_whose_receipt_claims_it(workspace_with_data):
    run = _run_with_unit(workspace_with_data)
    # A receipt is the only record of what a unit actually wrote, so it is the
    # index — nothing here is inferred from ordering or naming.
    receipts = store.run_dir(workspace_with_data, run["id"]) / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    body = {
        "artifact_refs": ["planning:apm"],
        "capability_id": "planning.apm_ready",
        "executor_id": "planning.apm",
        "unit_id": "apm",
        "workspace_revision_before": 1,
        "workspace_revision_after": 2,
        "output": {"status": "updated"},
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    (receipts / "apm.json").write_text(canonical, encoding="utf-8")
    import hashlib
    run["workflow"]["stages"][0]["units"][0]["receipt_sidecar"] = {
        "path": "receipts/apm.json",
        "unit_id": "apm",
        "payload_hash": "sha256:" + hashlib.sha256(canonical.encode()).hexdigest(),
    }
    store.save_run(workspace_with_data, run)

    located = provenance.resolve_artifact(workspace_with_data, "planning:apm")
    assert located == {"run_id": run["id"], "unit_id": "apm"}

    payload = provenance.artifact_provenance(workspace_with_data, "planning:apm")
    assert payload["state"] == "attributed"
    assert payload["unit"]["id"] == "apm"
    assert payload["receipt"]["artifact_refs"] == ["planning:apm"]
    assert payload["receipt"]["workspace_revision_after"] == 2
