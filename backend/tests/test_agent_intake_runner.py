"""Phase 10 gate for the retained folder-intake protocol runner (`P10.3`).

`IntakeRunner` is retained by an explicit decision record
(`docs/agent-protocol-runner-decisions.md`), not by default. These tests pin what
retention had to buy: the one model call goes through the registered
`intake.classification` worker and the shared gateway, it sees only what the
declared context preset permits, its proposal is durable before any approval, a
restart reuses that proposal instead of re-billing, the auditor's review of the
staged manifest is an editable approval batch, applying is idempotent, and the
model is optional.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time

import pytest

from app import intake, llm, workspaces
from app.agent import runner, store
from app.agent.context import PRESETS
from app.agent.intake_runner import (
    IntakeRunner,
    apply_unit_id,
    classification_unit_id,
)
from app.agent.workers import WORKERS
from app.agent.workers.intake import (
    CLASSIFICATION_WORKER_ID,
    supplied_files,
    validate_classification_proposal,
)

from conftest import wait_run


POLICY_TEXT = b"Procurement policy requires approval before commitment.\n"


def _sidecar(folder, unit_id):
    """Load one semantic unit's sidecar without restating its filename encoding."""

    matches = [
        path
        for path in sorted(folder.glob("*.json"))
        if json.loads(path.read_text()).get("unit_id") == unit_id
    ]
    assert matches, f"no sidecar for '{unit_id}' in {folder}"
    return json.loads(matches[0].read_text())


def _workspace():
    return workspaces.create_workspace("Intake Runner")


def _batch(ws, mode="auto", relative_path="Audit/guidance.txt", content=POLICY_TEXT):
    source = intake.create_source(ws, "Audit folder", "Client Audit")
    staged = intake.compare_manifest(
        ws,
        source["id"],
        [{"relative_path": relative_path, "size": len(content), "last_modified": 1}],
        mode,
    )["batch"]
    item = intake.requested_item(staged, relative_path)
    target = intake.staging_path(ws, staged, item)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    intake.mark_uploaded(
        ws, staged, item, hashlib.sha1(content).hexdigest(), len(content), target
    )
    return source, intake.complete_upload(ws, staged["id"])


def _classification(item_id, category="policy"):
    return {
        "items": [
            {
                "id": item_id,
                "route": "document",
                "document_category": category,
                "confidence": "high",
                "rationale": "The filename indicates audit guidance.",
                "proposed_action": "import",
            }
        ]
    }


def _start(ws, source, batch, mode="auto"):
    return runner.start_run(
        ws,
        mode,
        {"batch_id": batch["id"], "source_id": source["id"]},
        kind="intake",
    )


# --------------------------------------------------------------------------- #
# Declared context and the registered worker
# --------------------------------------------------------------------------- #
def test_classification_context_declares_file_metadata_only():
    spec = PRESETS.compile("intake.classification")

    assert [source.id for source in spec.sources] == ["staged_files"]
    assert [
        representation.kind for representation in spec.sources[0].representations
    ] == ["file_metadata"]
    assert spec.privacy.allow_file_metadata is True
    # Every other content class stays deny-by-default, so a staged file's
    # contents cannot reach the classifier through a second representation.
    assert spec.privacy.allow_document_text is False
    assert spec.privacy.allow_table_metadata is False
    assert spec.privacy.allow_table_profiles is False
    assert spec.privacy.allow_table_rows is False


def test_classification_worker_is_registered_with_hash_identity():
    definition = WORKERS.get(CLASSIFICATION_WORKER_ID)

    assert definition.prompt_hash.startswith("sha256:")
    assert definition.implementation_hash.startswith("sha256:")
    assert definition.response_schema.schema_hash.startswith("sha256:")
    assert definition.repair_policy.max_repair_attempts == 1


def test_intake_run_supplies_the_model_only_declared_file_metadata(fake_agent_llm):
    ws = _workspace()
    source, batch = _batch(ws)
    item_id = batch["items"][0]["id"]
    fake_agent_llm.overrides["agent:file_classification"] = _classification(item_id)

    run = _start(ws, source, batch)
    finished = wait_run(ws, run["id"])
    assert finished["status"] == "completed", finished.get("error")

    call = next(
        item for item in fake_agent_llm.calls if item["tag"] == "agent:file_classification"
    )
    supplied = json.loads(call["messages"][-1]["content"])
    assert [entry["id"] for entry in supplied["items"]] == [item_id]
    # Local technical metadata only: no staging path, no absolute path, and no
    # byte of the staged file itself.
    body = call["messages"][-1]["content"]
    assert "Procurement policy requires approval" not in body
    assert "Staging" not in body
    assert str(ws.root) not in body
    assert set(supplied["items"][0]) == {
        "id",
        "relative_path",
        "size",
        "last_modified",
        "mime",
        "state",
        "local_metadata",
        "deterministic",
    }


def test_classification_manifest_is_persisted_content_free_before_the_call(
    fake_agent_llm,
):
    ws = _workspace()
    source, batch = _batch(ws)
    item_id = batch["items"][0]["id"]
    fake_agent_llm.overrides["agent:file_classification"] = _classification(item_id)

    run = _start(ws, source, batch)
    wait_run(ws, run["id"])

    manifests = sorted((store.run_dir(ws, run["id"]) / "contexts").glob("*.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text())
    payload = json.dumps(manifest)
    assert manifest["capability_id"] == "intake.classification"
    assert manifest["selections"]
    assert all(
        selection["source_id"] == "staged_files" for selection in manifest["selections"]
    )
    assert "Procurement policy requires approval" not in payload
    assert "guidance.txt" not in payload


def test_worker_rejects_a_classification_for_an_unsupplied_file():
    class _Item:
        source_id = "staged_files"
        content = {"id": "itm_1", "relative_path": "Audit/a.txt"}

    class _Request:
        context = type("Bundle", (), {"items": (_Item(),)})()

    with pytest.raises(Exception) as error:
        validate_classification_proposal(
            {"items": [{"id": "itm_unknown", "route": "document"}]}, _Request()
        )
    assert "itm_unknown" in str(error.value)

    assert [entry["id"] for entry in supplied_files(_Request())] == ["itm_1"]


# --------------------------------------------------------------------------- #
# Durability: proposal before approval, reuse without re-billing
# --------------------------------------------------------------------------- #
def test_classification_proposal_and_accepted_decisions_are_durable(fake_agent_llm):
    ws = _workspace()
    source, batch = _batch(ws)
    item_id = batch["items"][0]["id"]
    fake_agent_llm.overrides["agent:file_classification"] = _classification(item_id)

    run = _start(ws, source, batch)
    finished = wait_run(ws, run["id"])
    assert finished["status"] == "completed", finished.get("error")

    proposals = store.run_dir(ws, run["id"]) / "proposals"
    classification = _sidecar(proposals, classification_unit_id(batch["id"]))
    assert classification["worker_id"] == CLASSIFICATION_WORKER_ID
    assert classification["execution_identity"]["prompt_hash"].startswith("sha256:")
    assert [entry["id"] for entry in classification["proposal"]["items"]] == [item_id]

    accepted = _sidecar(proposals, apply_unit_id(batch["id"]))
    assert accepted["status"] == "accepted"
    assert accepted["origin"] == "deterministic"
    assert [
        decision["item_id"] for decision in accepted["proposal"]["decisions"]
    ] == [item_id]


def test_a_repeated_classification_reuses_the_proposal_without_re_billing(
    fake_agent_llm,
):
    """The recovery boundary that matters: the provider call already happened.

    Classification is a proposal-only pipeline unit, so a run interrupted after
    the proposal was persisted resumes from the sidecar. The batch is reloaded
    from disk between the two attempts, which is exactly the state a restarted
    worker would see before ``apply`` has run.
    """
    ws = _workspace()
    _source, batch = _batch(ws)
    item_id = batch["items"][0]["id"]
    fake_agent_llm.overrides["agent:file_classification"] = _classification(item_id)
    record = store.new_run(
        ws, "auto", {"batch_id": batch["id"]}, kind="intake"
    )
    handle = runner.RunHandle(ws.id, record["id"])

    IntakeRunner(ws, record, handle)._classify(intake.load_batch(ws, batch["id"]))
    assert [call["tag"] for call in fake_agent_llm.calls] == ["agent:file_classification"]
    proposal = _sidecar(
        store.run_dir(ws, record["id"]) / "proposals",
        classification_unit_id(batch["id"]),
    )

    resumed = intake.load_batch(ws, batch["id"])
    IntakeRunner(ws, store.load_run(ws, record["id"]), handle)._classify(resumed)

    assert [call["tag"] for call in fake_agent_llm.calls] == ["agent:file_classification"]
    assert resumed["items"][0]["classification"]["document_category"] == "policy"
    assert (
        _sidecar(
            store.run_dir(ws, record["id"]) / "proposals",
            classification_unit_id(batch["id"]),
        )["execution_identity_hash"]
        == proposal["execution_identity_hash"]
    )


def test_a_changed_batch_regenerates_rather_than_reusing_the_proposal(fake_agent_llm):
    """Reuse is exact-identity, not "a proposal exists for this unit"."""

    ws = _workspace()
    _source, batch = _batch(ws)
    item_id = batch["items"][0]["id"]
    fake_agent_llm.overrides["agent:file_classification"] = _classification(item_id)
    record = store.new_run(ws, "auto", {"batch_id": batch["id"]}, kind="intake")
    handle = runner.RunHandle(ws.id, record["id"])

    IntakeRunner(ws, record, handle)._classify(intake.load_batch(ws, batch["id"]))

    changed = intake.load_batch(ws, batch["id"])
    changed["items"][0]["classification"]["route"] = "table"
    intake.save_batch(ws, changed)
    IntakeRunner(ws, store.load_run(ws, record["id"]), handle)._classify(
        intake.load_batch(ws, batch["id"])
    )

    assert [call["tag"] for call in fake_agent_llm.calls] == [
        "agent:file_classification",
        "agent:file_classification",
    ]


def test_classification_turn_is_charged_to_the_run_model_budget(fake_agent_llm):
    ws = _workspace()
    source, batch = _batch(ws)
    fake_agent_llm.overrides["agent:file_classification"] = _classification(
        batch["items"][0]["id"]
    )

    run = _start(ws, source, batch)
    finished = wait_run(ws, run["id"])

    assert finished["status"] == "completed", finished.get("error")
    assert finished["usage"]["llm_turns"] == 1
    assert int(finished["usage"]["estimated_prompt_tokens"]) > 0
    provenance = finished.get("model_provenance") or []
    assert all("Procurement policy" not in json.dumps(entry) for entry in provenance)


# --------------------------------------------------------------------------- #
# Review, application, and the optional model
# --------------------------------------------------------------------------- #
def test_permission_mode_review_is_an_editable_approval_batch(fake_agent_llm):
    ws = _workspace()
    source, batch = _batch(ws, mode="permission")
    item_id = batch["items"][0]["id"]
    fake_agent_llm.overrides["agent:file_classification"] = _classification(item_id)

    run = _start(ws, source, batch, mode="permission")
    deadline = time.monotonic() + 15
    approval = None
    while time.monotonic() < deadline:
        current = store.load_run(ws, run["id"])
        approval = next(
            (
                item
                for item in current.get("approvals") or []
                if item["status"] == "pending"
            ),
            None,
        )
        if approval is not None or current["status"] in store.TERMINAL_STATUSES:
            break
        time.sleep(0.02)
    assert approval is not None, "permission mode must gate the routing decisions"
    assert approval["kind"] == "file_classification"
    assert approval["items"][0]["spec"]["item_id"] == item_id

    runner.resolve_approval(
        ws,
        run["id"],
        approval["id"],
        [
            {
                "item_id": approval["items"][0]["id"],
                "action": "edit",
                "spec": {
                    **approval["items"][0]["spec"],
                    "document_category": "background",
                },
            }
        ],
    )
    finished = wait_run(ws, run["id"])

    assert finished["status"] == "completed", finished.get("error")
    accepted = _sidecar(
        store.run_dir(ws, run["id"]) / "proposals", apply_unit_id(batch["id"])
    )
    assert accepted["origin"] == "auditor_approved"
    assert accepted["proposal"]["decisions"][0]["document_category"] == "background"
    assert workspaces.load_workspace(ws.id).documents[0]["category"] == "background"


def test_apply_is_idempotent_for_an_already_completed_batch(fake_agent_llm):
    ws = _workspace()
    source, batch = _batch(ws)
    fake_agent_llm.overrides["agent:file_classification"] = _classification(
        batch["items"][0]["id"]
    )

    first = wait_run(ws, _start(ws, source, batch)["id"])
    assert first["status"] == "completed", first.get("error")
    assert len(workspaces.load_workspace(ws.id).documents) == 1

    second = wait_run(ws, _start(ws, source, batch)["id"])

    assert second["status"] == "completed", second.get("error")
    assert len(workspaces.load_workspace(ws.id).documents) == 1
    assert second["intake"]["imported"] == 1


def test_unusable_model_response_falls_back_to_deterministic_local_routing(
    fake_agent_llm,
):
    ws = _workspace()
    source, batch = _batch(ws)
    # Both the first attempt and the bounded repair name a file the worker was
    # never supplied, so the registry exhausts its allowance.
    fake_agent_llm.overrides["agent:file_classification"] = _classification("itm_bogus")

    finished = wait_run(ws, _start(ws, source, batch)["id"])

    assert finished["status"] == "completed", finished.get("error")
    assert (
        len(
            [
                call
                for call in fake_agent_llm.calls
                if call["tag"] == "agent:file_classification"
            ]
        )
        == 2
    ), "one bounded repair attempt, then fall back"
    assert any(
        "using local routing" in str(warning)
        for warning in finished.get("warnings") or []
    )
    # The deterministic route still imported the file.
    assert workspaces.load_workspace(ws.id).documents


def test_intake_runs_without_a_configured_model(monkeypatch):
    ws = _workspace()
    source, batch = _batch(ws)
    monkeypatch.setattr(
        llm,
        "agent_status",
        lambda: {"configured": False, "backend": "", "model": ""},
    )

    finished = wait_run(ws, _start(ws, source, batch)["id"])

    assert finished["status"] == "completed", finished.get("error")
    assert int(finished["usage"].get("llm_turns") or 0) == 0
    assert workspaces.load_workspace(ws.id).documents


def test_intake_runner_accepts_an_injected_runtime(workspace_with_data):
    run = store.new_run(workspace_with_data, "auto", kind="intake")
    handle = runner.RunHandle(workspace_with_data.id, run["id"])
    default = IntakeRunner(workspace_with_data, run, handle)
    lock = threading.RLock()
    injected = IntakeRunner(
        workspace_with_data,
        run,
        handle,
        runtime=default.runtime,
        state_lock=lock,
    )

    assert injected.runtime is default.runtime
    assert injected.unit_pipeline.runtime is default.runtime
